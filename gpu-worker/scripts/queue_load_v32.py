# -*- coding: utf-8 -*-
"""V32 FIFO 대기열 + 동시제출 부하검사 (Phase 3·4).

스테이징 전용 큐 시뮬레이션 실행기 (운영 감시원은 건드리지 않음):
  - N개 작업을 동시(또는 stagger_s 간격) 제출
  - FIFO: max_active_gpu_jobs=1 — GPU 세그먼트 단계는 한 번에 한 작업만
  - finish_overlap: 앞 작업의 finish(CPU) 중에 다음 작업의 scan/segment 시작 허용
  - 각 작업의 대기시간, ETA 추정치(범위)와 실제의 오차를 기록
ETA 모델 (실측 기반): scan 92~118s, seg(k12,warm) 100~165s, finish 45~114s
  → 대기 중 작업: ETA = 앞 작업 잔여 + 자기 처리 예상 (P25~P90 범위 제시)
산출: QUEUE_LOAD_REPORT.json / QUEUE_LOAD_REPORT.md
사용: python queue_load_v32.py --jobs 3 --k 12 --warm 12 [--stagger 0]
"""
import argparse, json, os, statistics, sys, threading, time, uuid

import modal
import requests

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
BENCH_PID_BASE = "beac0003-0000-4000-8000-0000000000"  # +NN (부하검사 전용 행)
SRC_PROJECT = "31118dec-b65d-4d99-b67e-61ab3333094b"
SRC_PATH = "4117e902-3396-4b14-aea0-957b326ab563/1786760197372_s94d16lfj6.mp4"

# 실측 기반 단계 예상 (초) — (p25, p50, p90). 2026-08-18 p1-final 실측으로 갱신
EST = {"scan": (45, 55, 95), "seg": (95, 120, 165), "finish": (23, 30, 40)}


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def ensure_project(pid, title):
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{pid}", "select": "id"}, headers=sbh(), timeout=30)
    r.raise_for_status()
    if r.json():
        return
    src = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{SRC_PROJECT}",
                               "select": "user_id,source_path,source_bytes,source_duration_sec,probe"},
                       headers=sbh(), timeout=30)
    src.raise_for_status()
    row = src.json()[0]
    ins = {"id": pid, "user_id": row["user_id"], "title": title,
           "source_path": row["source_path"], "source_bytes": row.get("source_bytes"),
           "source_duration_sec": row.get("source_duration_sec"), "probe": row.get("probe"),
           "objective": "wm_remove", "status": "wm_queued", "status_detail": "v32 queue-load",
           "wm_mode": "auto", "wm_tier": "fast"}
    requests.post(f"{SB_URL}/rest/v1/sc_projects",
                  headers=sbh({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                  data=json.dumps(ins), timeout=30).raise_for_status()


def clean_tmp(pid):
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": f"wmtmp-v32/{pid}", "limit": 200}), timeout=30)
    if not r.ok:
        return
    names = [f"wmtmp-v32/{pid}/" + o["name"] for o in r.json() if o.get("name")]
    if names:
        requests.request("DELETE", f"{SB_URL}/storage/v1/object/videos-clips",
                         headers=sbh({"Content-Type": "application/json"}),
                         data=json.dumps({"prefixes": names}), timeout=60)


def eta_range(pos_ahead_remaining_s, self_stage_done=0):
    """대기 중 작업의 예상 시작·완료 범위 (초). self_stage_done: 이미 끝난 자기 단계 수."""
    stages = ["scan", "seg", "finish"][self_stage_done:]
    lo = pos_ahead_remaining_s + sum(EST[s][0] for s in stages)
    mid = pos_ahead_remaining_s + sum(EST[s][1] for s in stages)
    hi = pos_ahead_remaining_s + sum(EST[s][2] for s in stages)
    return lo, mid, hi


class FifoRunner:
    """max_active_gpu_jobs=1 FIFO + finish_overlap 큐 실행기."""

    def __init__(self, k, warm, key_step):
        self.k, self.warm, self.key_step = k, warm, key_step
        # 공정한 선착순: 제출 순서표(ticket) — threading.Lock은 순서를 보장하지 않음
        self.cv = threading.Condition()
        self.next_serve = 0
        self.abandoned = set()   # scan 실패 등으로 차례를 포기한 작업
        self.scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
        self.seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
        self.fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")

    def run_job(self, idx, pid, t_submit, rec):
        rec["t_submit"] = t_submit
        # scan은 GPU 락 밖 (CPU 자원) — finish_overlap과 동일 논리로 겹침 허용
        t0 = time.time()
        rec["wait_before_scan_s"] = round(t0 - t_submit, 1)
        scan = self.scan_fn.remote({"input": {"project_id": pid, "phase": "scan_v32",
                                              "seg_k": self.k}})
        if scan.get("error") or scan.get("note"):
            rec["result"] = "SCAN_FAIL"; rec["scan"] = scan
            with self.cv:                  # 차례 포기 등록 (실패해도 뒷사람 진행)
                self.abandoned.add(idx)
                while self.next_serve in self.abandoned:
                    self.next_serve += 1
                self.cv.notify_all()
            return
        rec["t_scan_done"] = round(time.time() - t_submit, 1)
        with self.cv:                      # FIFO: 제출 순서(idx)대로 GPU 차례를 기다림
            while self.next_serve != idx:
                self.cv.wait(timeout=5)
        try:
            rec["gpu_wait_s"] = round(time.time() - t_submit - rec["t_scan_done"], 1)
            segs = [self.seg_fn.spawn({"input": {"project_id": pid, "phase": "segment_v32",
                                                 "part": p, "key_step": self.key_step}})
                    for p in range(self.k)]
            fin_call = self.fin_fn.spawn({"input": {"project_id": pid, "phase": "finish_v32",
                                                    "parts": self.k, "t0": t0, "stream": True}})
            seg_err = 0
            for c in segs:
                try:
                    o = c.get(timeout=1500)
                    if o.get("error"):
                        seg_err += 1
                except Exception:
                    seg_err += 1
            rec["t_segments_done"] = round(time.time() - t_submit, 1)
            rec["seg_errors"] = seg_err
        finally:
            with self.cv:
                if self.next_serve == idx:
                    self.next_serve = idx + 1
                while self.next_serve in self.abandoned:
                    self.next_serve += 1
                self.cv.notify_all()
        # finish는 차례 반납 후 대기 (finish_overlap — 다음 작업 GPU 시작 허용)
        try:
            fin = fin_call.get(timeout=900)
        except Exception as e:
            fin = {"error": str(e)[:200]}
        rec["finish_ok"] = bool(fin.get("ok"))
        rec["up_mode"] = fin.get("up_mode")
        rec["total_s"] = round(time.time() - t_submit, 1)
        rec["result"] = "OK" if fin.get("ok") and not seg_err else "FAIL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--warm", type=int, default=12)
    ap.add_argument("--key_step", type=int, default=5)
    ap.add_argument("--stagger", type=float, default=0.0)
    ap.add_argument("--label", default="queue")
    a = ap.parse_args()

    runner = FifoRunner(a.k, a.warm, a.key_step)
    # 예열은 1회 (운영과 동일 — 작업마다 예열하지 않음)
    warm_calls = [runner.seg_fn.spawn({"input": {"phase": "warm_v32"}}) for _ in range(a.warm)]

    recs = []
    threads = []
    t_all0 = time.time()
    for i in range(a.jobs):
        pid = f"{BENCH_PID_BASE}{i+1:02d}"
        ensure_project(pid, f"[v32-queue] job{i+1}")
        clean_tmp(pid)
        rec = {"job": i + 1, "pid": pid}
        # 제출 시점 ETA 기록 (앞 작업 잔여 = 앞 작업 수 × seg 중앙값 — FIFO GPU 직렬 기준)
        ahead = i * EST["seg"][1]
        lo, mid, hi = eta_range(ahead)
        rec["eta_at_submit"] = {"lo_s": lo, "mid_s": mid, "hi_s": hi}
        recs.append(rec)
        t_sub = time.time()
        th = threading.Thread(target=runner.run_job, args=(i, pid, t_sub, rec))
        th.start()
        threads.append(th)
        if a.stagger:
            time.sleep(a.stagger)
    for th in threads:
        th.join()
    for c in warm_calls:
        try: c.get(timeout=5)
        except Exception: pass

    oks = [r for r in recs if r.get("result") == "OK"]
    totals = sorted(r["total_s"] for r in oks)
    for r in recs:
        if r.get("total_s") and r.get("eta_at_submit"):
            e = r["eta_at_submit"]
            r["eta_in_range"] = bool(e["lo_s"] * 0.8 <= r["total_s"] <= e["hi_s"] * 1.2)
            r["eta_err_pct"] = round(100 * (r["total_s"] - e["mid_s"]) / e["mid_s"], 1)
    summary = {"label": a.label, "jobs": a.jobs, "k": a.k, "warm": a.warm,
               "success": len(oks), "fail": a.jobs - len(oks),
               "wall_all_s": round(time.time() - t_all0, 1)}
    if totals:
        summary.update({"p50": round(statistics.median(totals), 1),
                        "min": totals[0], "max": totals[-1],
                        "p95": totals[max(0, int(len(totals) * 0.95) - 1)]
                        if len(totals) > 1 else totals[-1],
                        "eta_in_range": sum(1 for r in recs if r.get("eta_in_range")),
                        "eta_err_pct_mean": round(statistics.mean(
                            abs(r["eta_err_pct"]) for r in recs if "eta_err_pct" in r), 1)})
    out = {"summary": summary, "records": recs}
    json.dump(out, open("QUEUE_LOAD_REPORT.json", "w"), ensure_ascii=False, indent=1, default=str)
    lines = [f"# V32 FIFO 대기열 부하검사 — {a.jobs}건 동시 제출", "",
             f"성공 {summary['success']}/{a.jobs}, P50 {summary.get('p50')}s, "
             f"최장 {summary.get('max')}s, ETA 범위적중 {summary.get('eta_in_range')}/{a.jobs}", "",
             "| job | 총시간 | GPU대기 | ETA중앙 | 오차% | 결과 |", "|---|---|---|---|---|---|"]
    for r in recs:
        lines.append(f"| {r['job']} | {r.get('total_s')} | {r.get('gpu_wait_s')} | "
                     f"{r.get('eta_at_submit', {}).get('mid_s')} | {r.get('eta_err_pct')} | "
                     f"{r.get('result')} |")
    open("QUEUE_LOAD_REPORT.md", "w").write("\n".join(lines) + "\n")
    print("[SUMMARY]", json.dumps(summary, ensure_ascii=False))
    if not oks:
        sys.exit(1)


if __name__ == "__main__":
    main()
