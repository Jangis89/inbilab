# -*- coding: utf-8 -*-
"""V32 영속 FIFO 대기열 서비스 (Phase E — staging 전용, 운영 감시원 미접촉).

test harness(스레드 락)가 아니라 DB 기반 실제 서비스 경로:
  - 영속 FIFO: Supabase 테이블 wm_v32_queue (프로세스 재시작에도 순서 보존)
  - 접수(enqueue): 같은 project가 대기/실행 중이면 중복 거부 (idempotent)
  - claim: 조건부 UPDATE(status=queued → running + attempt_token) — 러너 중복 실행 방지
  - 재시작 복구: heartbeat가 stale한 running 행을 queued로 되돌림 (새 attempt_token)
  - 취소: queued/running 어느 단계든 cancelled 처리, GPU 단계 사이에서 중단
  - finish overlap: 세그 완료 즉시 다음 작업 claim, finish는 백그라운드 대기
  - ETA: 위치 × 실측 단계표로 lo/mid/hi 범위 기록

테이블 (1회 생성 — docs/QUEUE_TABLE.sql):
  create table if not exists wm_v32_queue (
    id bigint generated always as identity primary key,
    project_id uuid not null,
    status text not null default 'queued',
    attempt_token uuid,
    submitted_at timestamptz not null default now(),
    started_at timestamptz, finished_at timestamptz,
    heartbeat_at timestamptz,
    eta_lo_s int, eta_mid_s int, eta_hi_s int,
    result text, error text);
  create unique index if not exists wm_v32_queue_active_uniq
    on wm_v32_queue(project_id) where status in ('queued','running','finishing');
"""
import argparse, json, os, sys, threading, time, uuid

import modal
import requests

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
TBL = f"{SB_URL}/rest/v1/wm_v32_queue"
EST = {"scan": (45, 55, 95), "seg": (95, 120, 165), "finish": (23, 30, 40)}
K = 12
KEY_STEP = 5
PREWARM = 3          # request-time prewarm (permanent 아님)
STALE_S = 120        # heartbeat 이보다 오래되면 죽은 러너로 간주


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def enqueue(project_id):
    """접수 (중복이면 409 반환값 False)."""
    pos = queue_depth()
    ahead = pos * EST["seg"][1]
    lo = ahead + sum(v[0] for v in EST.values())
    mid = ahead + sum(v[1] for v in EST.values())
    hi = ahead + sum(v[2] for v in EST.values())
    r = requests.post(TBL, headers=sbh({"Content-Type": "application/json",
                                        "Prefer": "return=representation"}),
                      data=json.dumps({"project_id": project_id,
                                       "eta_lo_s": lo, "eta_mid_s": mid, "eta_hi_s": hi}),
                      timeout=30)
    if r.status_code == 409:
        return None    # 중복 접수 거부 (unique partial index)
    r.raise_for_status()
    return r.json()[0]


def queue_depth():
    r = requests.get(TBL, params={"status": "in.(queued,running,finishing)",
                                  "select": "id"}, headers=sbh(), timeout=30)
    r.raise_for_status()
    return len(r.json())


def cancel(queue_id):
    r = requests.patch(TBL, params={"id": f"eq.{queue_id}",
                                    "status": "in.(queued,running)"},
                       headers=sbh({"Content-Type": "application/json",
                                    "Prefer": "return=representation"}),
                       data=json.dumps({"status": "cancelled",
                                        "finished_at": "now()"}), timeout=30)
    r.raise_for_status()
    return len(r.json()) > 0


def recover_stale():
    """죽은 러너의 running 작업을 queued로 복구 (재시작 복구)."""
    import datetime
    cut = (datetime.datetime.utcnow()
           - datetime.timedelta(seconds=STALE_S)).isoformat() + "Z"
    r = requests.patch(TBL, params={"status": "in.(running,finishing)",
                                    "heartbeat_at": f"lt.{cut}"},
                       headers=sbh({"Content-Type": "application/json",
                                    "Prefer": "return=representation"}),
                       data=json.dumps({"status": "queued", "attempt_token": None,
                                        "started_at": None}), timeout=30)
    r.raise_for_status()
    return [row["id"] for row in r.json()]


def claim_next(token):
    """FIFO 선두 1건 조건부 claim — 다른 러너와 경합해도 한쪽만 성공."""
    r = requests.get(TBL, params={"status": "eq.queued", "select": "id,project_id",
                                  "order": "submitted_at.asc", "limit": "1"},
                     headers=sbh(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    qid = rows[0]["id"]
    r2 = requests.patch(TBL, params={"id": f"eq.{qid}", "status": "eq.queued"},
                        headers=sbh({"Content-Type": "application/json",
                                     "Prefer": "return=representation"}),
                        data=json.dumps({"status": "running", "attempt_token": token,
                                         "started_at": "now()",
                                         "heartbeat_at": "now()"}), timeout=30)
    r2.raise_for_status()
    got = r2.json()
    return {"id": qid, "project_id": rows[0]["project_id"]} if got else None


def _hb(qid, stop):
    while not stop.is_set():
        try:
            requests.patch(TBL, params={"id": f"eq.{qid}"},
                           headers=sbh({"Content-Type": "application/json"}),
                           data=json.dumps({"heartbeat_at": "now()"}), timeout=15)
        except Exception:
            pass
        stop.wait(20)


def _status(qid):
    r = requests.get(TBL, params={"id": f"eq.{qid}", "select": "status"},
                     headers=sbh(), timeout=15)
    return (r.json() or [{}])[0].get("status")


def _finish_status(qid, ok, err=""):
    requests.patch(TBL, params={"id": f"eq.{qid}"},
                   headers=sbh({"Content-Type": "application/json"}),
                   data=json.dumps({"status": "done" if ok else "failed",
                                    "finished_at": "now()",
                                    "result": "OK" if ok else "FAIL",
                                    "error": err[:300]}), timeout=30)


def run_service(max_jobs=None, idle_exit_s=180):
    """러너 본체: claim → prewarm+scan → segs → (finish 백그라운드) → 다음 claim."""
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")
    token = str(uuid.uuid4())
    done_ct = 0
    idle_since = time.time()
    fin_waiters = []
    print(f"[qsvc] 러너 시작 token={token[:8]}")
    recovered = recover_stale()
    if recovered:
        print(f"[qsvc] 재시작 복구: {recovered} → queued")
    while True:
        job = claim_next(token)
        if job is None:
            if fin_waiters:
                fin_waiters = [t for t in fin_waiters if t.is_alive()]
            if time.time() - idle_since > idle_exit_s and not fin_waiters:
                print("[qsvc] 유휴 종료")
                break
            time.sleep(3)
            continue
        idle_since = time.time()
        qid, pid = job["id"], job["project_id"]
        print(f"[qsvc] job#{qid} {pid} 시작")
        stop = threading.Event()
        hb = threading.Thread(target=_hb, args=(qid, stop), daemon=True)
        hb.start()
        try:
            warm = [seg_fn.spawn({"input": {"phase": "warm_v32"}}) for _ in range(PREWARM)]
            t0 = time.time()
            scan = scan_fn.remote({"input": {"project_id": pid, "phase": "scan_v32",
                                             "seg_k": K}})
            if scan.get("error") or scan.get("note"):
                _finish_status(qid, False, str(scan)[:200]); stop.set(); continue
            if _status(qid) == "cancelled":
                print(f"[qsvc] job#{qid} scan 후 취소 확인 — GPU 미진입")
                stop.set(); continue
            segs = [seg_fn.spawn({"input": {"project_id": pid, "phase": "segment_v32",
                                            "part": p, "key_step": KEY_STEP}})
                    for p in range(K)]
            fin_call = fin_fn.spawn({"input": {"project_id": pid, "phase": "finish_v32",
                                               "parts": K, "t0": t0, "stream": True}})
            err_ct = 0
            for c in segs:
                try:
                    o = c.get(timeout=1500)
                    if o.get("error"):
                        err_ct += 1
                except Exception:
                    err_ct += 1
            # finish overlap: 세그 끝나면 finishing 표시 후 다음 작업 claim 가능
            requests.patch(TBL, params={"id": f"eq.{qid}"},
                           headers=sbh({"Content-Type": "application/json"}),
                           data=json.dumps({"status": "finishing"}), timeout=30)
            def _wait_fin(qid=qid, fin_call=fin_call, err_ct=err_ct, stop=stop):
                try:
                    fin = fin_call.get(timeout=900)
                except Exception as e:
                    fin = {"error": str(e)[:200]}
                ok = bool(fin.get("ok")) and err_ct == 0
                _finish_status(qid, ok, str(fin.get("error") or ""))
                stop.set()
                print(f"[qsvc] job#{qid} finish {'OK' if ok else 'FAIL'}")
            th = threading.Thread(target=_wait_fin, daemon=True)
            th.start()
            fin_waiters.append(th)
            done_ct += 1
            if max_jobs and done_ct >= max_jobs:
                for t in fin_waiters:
                    t.join(timeout=900)
                print("[qsvc] max_jobs 도달 종료")
                break
        except Exception as e:
            _finish_status(qid, False, f"{type(e).__name__}: {e}")
            stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["service", "enqueue", "cancel", "status"],
                    default="service")
    ap.add_argument("--project", default="")
    ap.add_argument("--qid", type=int, default=0)
    ap.add_argument("--max_jobs", type=int, default=0)
    a = ap.parse_args()
    if a.mode == "enqueue":
        row = enqueue(a.project)
        print(json.dumps({"enqueued": bool(row), "row": row}, default=str))
    elif a.mode == "cancel":
        print(json.dumps({"cancelled": cancel(a.qid)}))
    elif a.mode == "status":
        r = requests.get(TBL, params={"select": "*", "order": "id.desc", "limit": "20"},
                         headers=sbh(), timeout=30)
        print(json.dumps(r.json(), indent=1, default=str))
    else:
        run_service(max_jobs=a.max_jobs or None)


if __name__ == "__main__":
    main()
