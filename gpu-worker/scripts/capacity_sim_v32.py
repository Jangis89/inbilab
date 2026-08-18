# -*- coding: utf-8 -*-
"""V32 원가·용량 시뮬레이터 (Phase 6) — discrete-event.

모델 (실측 분포 기반):
  scan: CPU 상자, 실측 92~118s (병렬 다운로드 개선 반영 옵션)
  segment: 공용 segment queue → warm 컨테이너 즉시, cold는 기동지연 후 합류
           delayed hedge: 실행이 hedge_s를 넘으면 여분 컨테이너에 복제, 먼저 끝난 쪽 채택
  finish: 스트리밍 — 마지막 세그 완료 후 tail(실측 dl_tail+concat+up) 소요
비용: GPU 초당 단가 × (실행 + warm 유휴) — 단가는 인자로 (기본값은 코드 상수, 결제 전 확인 필요)

사용:
  python capacity_sim_v32.py --dist docs/V32_WORKER_ALLOCATION_DISTRIBUTION.csv \
      --burst 9 --k 12 --trials 500
스윕: warm 0/1/2/3/4/6 × burst 9/12 × hedge off/15/30/60/120 → UNIT_ECONOMICS_CAPACITY.csv
"""
import argparse, csv, heapq, json, random, statistics, sys

# 실측 기본값 (2026-08-18 p1-stream 런) — dist CSV가 있으면 exec 분포는 CSV 우선
SCAN_S = (91.6, 103.9, 117.9)
FINISH_TAIL_S = (45.0, 50.0, 60.0)     # S3 멀티파트 적용 가정 (미적용이면 98~114)
FINISH_TAIL_S_PUT = (98.7, 108.5, 113.7)
COLD_START_S = (25.0, 56.0, 112.0)     # alloc_wait 실측 (min/med/max 근사 삼각분포)
MODEL_LOAD_S = 7.5
GPU_PRICE_PER_S = 1.95 / 3600.0        # L40S 시간당 $1.95 가정 — 결제 전 실제 단가 확인 필요
MAX_CONTAINERS = 32


def tri(lo, mid, hi, rng):
    return rng.triangular(lo, hi, mid)


def load_exec_dist(path, k=12, ks=5, extra_json=None):
    """현행 구성(k=12, ks=5)의 exec 분포만 사용 — 옛 구성 혼입 방지."""
    vals = []
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                try:
                    if int(row.get("k") or 0) != k or int(row.get("key_step") or 0) != ks:
                        continue
                    v = float(row["exec_s"])
                    if 20 < v < 400:
                        vals.append(v)
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    fresh = []
    if extra_json:
        try:
            for rec in json.load(open(extra_json)):
                for sg in rec.get("segments") or []:
                    if isinstance(sg, dict) and sg.get("exec_wall_s"):
                        fresh.append(float(sg["exec_wall_s"]))
        except OSError:
            pass
    # 최신 계측(worker 내부 exec_wall)이 30표본 이상이면 그것만 사용 —
    # 옛 exec_s는 혼잡·다운로드 경합이 섞여 있어 큐 모델과 이중계산됨
    if len(fresh) >= 30:
        return fresh
    return (vals + fresh) or [60, 65, 70, 75, 80, 90, 110, 140]


def simulate(rng, exec_dist, burst, k, warm, hedge_s, s3=True, arrival_spread_s=0.0):
    """burst개 작업 동시(또는 spread 내 균등) 제출. 공용 seg queue + work steal + hedge.
    반환: (작업별 완료시간 리스트, gpu_busy_s, warm_idle_s)"""
    jobs = []
    for j in range(burst):
        t_sub = rng.uniform(0, arrival_spread_s) if arrival_spread_s else 0.0
        t_scan_done = t_sub + tri(*SCAN_S, rng)
        segs = [tri(min(exec_dist), statistics.median(exec_dist), max(exec_dist), rng)
                if len(exec_dist) < 30 else rng.choice(exec_dist) for _ in range(k)]
        jobs.append({"j": j, "t_ready": t_scan_done, "segs": segs,
                     "done": [None] * k, "t_sub": t_sub})
    # 이벤트: (time, kind, data)
    # 컨테이너 상태: 가용 시각 목록. warm은 t=0 가용, cold는 첫 수요 시 기동
    containers = [0.0] * warm           # 가용 시각
    cold_used = 0
    # 공용 큐: (ready_time, job, seg_idx) — ready 순
    tasks = []
    for job in jobs:
        for si in range(k):
            heapq.heappush(tasks, (job["t_ready"], job["j"], si))
    gpu_busy = 0.0
    running = []  # (finish_time, job_j, seg_idx, start_time, is_hedge)
    hedged = set()
    while tasks or running:
        # 다음 task를 배정할 수 있는가
        if tasks:
            t_ready, jj, si = tasks[0]
            # 가용 컨테이너 중 가장 빠른 것
            if containers:
                ci = min(range(len(containers)), key=lambda i: containers[i])
                t_avail = containers[ci]
            else:
                ci, t_avail = None, float("inf")
            # cold 기동 옵션
            can_cold = (warm + cold_used) < MAX_CONTAINERS
            t_cold = t_ready + tri(*COLD_START_S, rng) + MODEL_LOAD_S if can_cold else float("inf")
            t_start_warm = max(t_ready, t_avail)
            if t_cold < t_start_warm and can_cold:
                heapq.heappop(tasks)
                dur = jobs[jj]["segs"][si]
                t_end = t_cold + dur
                containers.append(t_end)
                cold_used += 1
                gpu_busy += dur
                heapq.heappush(running, (t_end, jj, si, t_cold, False))
                continue
            if t_avail < float("inf"):
                heapq.heappop(tasks)
                dur = jobs[jj]["segs"][si]
                t_end = t_start_warm + dur
                containers[ci] = t_end
                gpu_busy += dur
                heapq.heappush(running, (t_end, jj, si, t_start_warm, False))
                continue
        if running:
            t_end, jj, si, t_start, is_hedge = heapq.heappop(running)
            job = jobs[jj]
            if job["done"][si] is None or t_end < job["done"][si]:
                job["done"][si] = t_end
            # delayed hedge: 완료 전 hedge_s 초과분 검사 — 간이 구현:
            # 실행이 hedge_s를 넘겼고 아직 미완료였던 경우 복제본이 이미 발사됐다고 보고
            # 복제 실행시간을 새로 뽑아 더 이른 쪽을 채택
            if hedge_s and not is_hedge and (t_end - t_start) > hedge_s \
                    and (jj, si) not in hedged:
                hedged.add((jj, si))
                dur2 = rng.choice(exec_dist) if len(exec_dist) >= 30 else \
                    tri(min(exec_dist), statistics.median(exec_dist), max(exec_dist), rng)
                t2 = t_start + hedge_s + dur2
                gpu_busy += dur2
                if t2 < job["done"][si]:
                    job["done"][si] = t2
    totals = []
    for job in jobs:
        t_last = max(job["done"])
        tail = tri(*(FINISH_TAIL_S if s3 else FINISH_TAIL_S_PUT), rng)
        totals.append(t_last + tail - job["t_sub"])
    # warm 유휴비용: warm 컨테이너는 마지막 사용 시각까지 (scaledown 300s 근사 미포함, 실험창 기준)
    horizon = max(totals) if totals else 0
    warm_idle = max(0.0, warm * horizon - gpu_busy) if warm else 0.0
    return totals, gpu_busy, warm_idle


def pct(v, p):
    s = sorted(v)
    return s[min(len(s) - 1, max(0, int(len(s) * p / 100) - (0 if p < 100 else 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default="docs/V32_WORKER_ALLOCATION_DISTRIBUTION.csv")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--seed", type=int, default=32)
    ap.add_argument("--out", default="UNIT_ECONOMICS_CAPACITY.csv")
    ap.add_argument("--price_per_h", type=float, default=1.95)
    ap.add_argument("--extra_json", default=None)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    exec_dist = load_exec_dist(a.dist, extra_json=a.extra_json)
    print(f"[sim] exec 분포 {len(exec_dist)}개 표본, med={statistics.median(exec_dist):.1f}s")
    price = a.price_per_h / 3600.0
    rows = []
    for burst in (1, 3, 5, 9, 12):
        for warm in (0, 1, 2, 3, 4, 6):
            for hedge in (0, 15, 30, 60, 120):
                allt, busy_l, idle_l = [], [], []
                for _ in range(a.trials):
                    t, b, i = simulate(rng, exec_dist, burst, a.k, warm, hedge)
                    allt.extend(t); busy_l.append(b); idle_l.append(i)
                busy = statistics.mean(busy_l); idle = statistics.mean(idle_l)
                cost_job = (busy + idle) * price / burst
                rows.append({"burst": burst, "warm": warm, "hedge_s": hedge,
                             "p50_s": round(pct(allt, 50), 1),
                             "p95_s": round(pct(allt, 95), 1),
                             "gpu_busy_s": round(busy, 0),
                             "warm_idle_s": round(idle, 0),
                             "cost_per_job_usd": round(cost_job, 4)})
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    # 요약: 각 burst에서 P95<=480 만족하는 최저비용 구성
    print("\n[sim] burst별 P95<=480 최저비용 구성:")
    for burst in (1, 3, 5, 9, 12):
        cand = [r for r in rows if r["burst"] == burst and r["p95_s"] <= 480]
        if cand:
            best = min(cand, key=lambda r: r["cost_per_job_usd"])
            print(f"  burst={burst}: warm={best['warm']} hedge={best['hedge_s']} "
                  f"P50={best['p50_s']} P95={best['p95_s']} ${best['cost_per_job_usd']}/작업")
        else:
            print(f"  burst={burst}: 만족 구성 없음(파라미터 확장 필요)")
    print(f"[sim] {a.out} 저장 ({len(rows)} 구성)")


if __name__ == "__main__":
    main()
