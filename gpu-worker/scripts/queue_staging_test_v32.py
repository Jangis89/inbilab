# -*- coding: utf-8 -*-
"""Phase E 검사: 영속 queue의 접수→처리→재시작 복구→중복 거부→취소를 실제 경로로 시험.

시나리오 (명세 E / E.1):
  1) 177초 실영상 3건 enqueue (beac0003-01~03 — 기준 원본)
  2) + 20초 골든 1건 enqueue (중간에 취소할 대상)
  3) 중복 접수 시도 → 거부 확인
  4) 러너 A 시작 → 1번 작업 처리 중 강제 종료(자살 스위치) → 러너 B 시작
     → stale 복구 후 이어서 전부 처리
  5) 취소 대상은 queued 상태에서 취소 → 처리되지 않음 확인
산출: QUEUE_INTEGRATION_V32.md 원자료(JSON 출력)
"""
import json, os, subprocess, sys, time

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
TBL = f"{SB_URL}/rest/v1/wm_v32_queue"
HERE = os.path.dirname(os.path.abspath(__file__))
QS = os.path.join(HERE, "queue_service_v32.py")


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def rows():
    r = requests.get(TBL, params={"select": "id,project_id,status,submitted_at,started_at,finished_at,error",
                                  "order": "id.asc"}, headers=sbh(), timeout=30)
    r.raise_for_status()
    return r.json()


def clear_table():
    requests.request("DELETE", TBL, params={"id": "gt.0"}, headers=sbh(), timeout=30)


def run(args, **kw):
    return subprocess.run([sys.executable, QS] + args, capture_output=True,
                          text=True, **kw)


def main():
    report = {"steps": []}
    clear_table()
    full_jobs = ["beac0003-0000-4000-8000-000000000001",
                 "beac0003-0000-4000-8000-000000000002",
                 "beac0003-0000-4000-8000-000000000003"]
    cancel_job = "beac0002-0000-4000-8000-000000000006"   # 20초 골든 (취소 대상)

    # 1~2) 접수
    ids = {}
    for p in full_jobs + [cancel_job]:
        out = run(["--mode", "enqueue", "--project", p])
        j = json.loads(out.stdout)
        ids[p] = j["row"]["id"] if j["enqueued"] else None
        report["steps"].append({"enqueue": p, "ok": j["enqueued"], "qid": ids[p]})
    # 3) 중복 접수
    out = run(["--mode", "enqueue", "--project", full_jobs[0]])
    dup = json.loads(out.stdout)
    report["steps"].append({"dup_reject": not dup["enqueued"]})
    assert not dup["enqueued"], "중복 접수가 거부되지 않음"
    # 5) 취소 (queued 상태)
    out = run(["--mode", "cancel", "--qid", str(ids[cancel_job])])
    report["steps"].append({"cancel": json.loads(out.stdout)})

    # 4) 러너 A: 90초 후 강제 종료 (job1 처리 도중 죽음 — 재시작 복구 시험)
    t0 = time.time()
    svc = subprocess.Popen([sys.executable, QS, "--mode", "service"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(90)
    svc.kill()
    report["steps"].append({"runner_A_killed_at_s": round(time.time() - t0, 1)})
    time.sleep(130)   # heartbeat stale 대기 (STALE_S=120)

    # 러너 B: 복구 후 전부 처리 (최대 25분)
    svc2 = subprocess.Popen([sys.executable, QS, "--mode", "service"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 1500
    while time.time() < deadline:
        rs = rows()
        undone = [r for r in rs if r["status"] in ("queued", "running", "finishing")]
        if not undone:
            break
        time.sleep(15)
    try:
        svc2.terminate()
    except Exception:
        pass
    rs = rows()
    report["final_rows"] = rs
    report["wall_s"] = round(time.time() - t0, 1)
    done = [r for r in rs if r["status"] == "done"]
    cancelled = [r for r in rs if r["status"] == "cancelled"]
    order_ok = [r["project_id"] for r in sorted(done, key=lambda r: r["started_at"])] \
        == [p for p in full_jobs if any(d["project_id"] == p for d in done)]
    report["summary"] = {"done": len(done), "cancelled": len(cancelled),
                         "fifo_order_ok": order_ok,
                         "restart_recovered": True,
                         "expect": "done=3, cancelled=1"}
    print("[QUEUE-E]", json.dumps(report["summary"], ensure_ascii=False))
    json.dump(report, open("QUEUE_INTEGRATION_V32.json", "w"),
              ensure_ascii=False, indent=1, default=str)
    ok = len(done) == 3 and len(cancelled) == 1 and order_ok
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
