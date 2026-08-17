# -*- coding: utf-8 -*-
"""V31 스테이징 벤치마크 오케스트레이터.

GitHub Actions에서 실행 (MODAL_TOKEN_ID/SECRET + SUPABASE_URL/SUPABASE_SERVICE_ROLE env 필요).
운영과 분리된 전용 벤치 프로젝트 행(sc_projects)을 사용하고, wmtmp-v31 prefix만 쓴다.

사용:
  python scripts/benchmark_v31.py --runs 3 --k 5 --warm 5
  python scripts/benchmark_v31.py --runs 1 --k 5 --warm 0 --label cold
"""
import argparse, json, os, statistics, sys, time, uuid

import modal
import requests

APP = "inbilab-wm-gpu-v31-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]

# 감사 기준 영상(사장님 본인 계정 시험 프로젝트 31118dec의 원본)을 참조하는 전용 벤치 행
BENCH_PID = "beac0001-0000-4000-8000-000000000031"
SRC_PROJECT = "31118dec-b65d-4d99-b67e-61ab3333094b"


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra: h.update(extra)
    return h


def ensure_bench_project():
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{BENCH_PID}", "select": "id"}, headers=sbh(), timeout=30)
    r.raise_for_status()
    if r.json():
        return
    src = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{SRC_PROJECT}",
                               "select": "user_id,title,source_path,source_bytes,source_duration_sec,probe"},
                       headers=sbh(), timeout=30)
    src.raise_for_status()
    row = src.json()[0]
    ins = {"id": BENCH_PID, "user_id": row["user_id"],
           "title": "[v31-bench] " + (row.get("title") or ""),
           "source_path": row["source_path"], "source_bytes": row.get("source_bytes"),
           "source_duration_sec": row.get("source_duration_sec"), "probe": row.get("probe"),
           "objective": "wm_remove", "status": "wm_queued", "status_detail": "v31 bench",
           "wm_mode": "auto", "wm_tier": "fast"}
    r2 = requests.post(f"{SB_URL}/rest/v1/sc_projects",
                       headers=sbh({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                       data=json.dumps(ins), timeout=30)
    r2.raise_for_status()
    print(f"[bench] 벤치 프로젝트 행 생성: {BENCH_PID}")


def clean_tmp():
    # wmtmp-v31/{BENCH_PID}/ 아래 잔여물 삭제 (있으면)
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": f"wmtmp-v31/{BENCH_PID}", "limit": 200}), timeout=30)
    if not r.ok:
        return
    names = [f"wmtmp-v31/{BENCH_PID}/" + o["name"] for o in r.json() if o.get("name")]
    if names:
        requests.request("DELETE", f"{SB_URL}/storage/v1/object/videos-clips",
                         headers=sbh({"Content-Type": "application/json"}),
                         data=json.dumps({"prefixes": names}), timeout=60)


def one_run(k, warm_n, run_id, plan_on="cpu"):
    plan_fn = modal.Function.from_name(APP, "plan_v31_gpu" if plan_on == "gpu" else "plan_v31_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v31_gpu")
    fin_fn = modal.Function.from_name(APP, "finish_v31_cpu")

    rec = {"run_id": run_id, "k": k, "warm_req": warm_n, "app": APP, "plan_on": plan_on, "t_start": time.time()}
    t0 = time.time()

    plan = plan_fn.remote({"input": {"project_id": BENCH_PID, "phase": "plan_v31", "seg_k": k}})
    rec["plan"] = plan
    if plan.get("error") or plan.get("note"):
        rec["result"] = "PLAN_FAIL"
        rec["total_s"] = round(time.time() - t0, 1)
        return rec
    rec["t_plan_done"] = round(time.time() - t0, 1)

    # 예열은 segment 함수 자체를 데운다 (다른 함수를 데우면 풀이 달라 무의미).
    # plan이 길어 미리 데우면 scaledown으로 식으므로 plan 완료 직후에 데운다.
    if warm_n > 0:
        warm_calls = [seg_fn.spawn({"input": {"phase": "warm_v31"}}) for _ in range(warm_n)]
        warm_results = []
        for c in warm_calls:
            try:
                warm_results.append(c.get(timeout=600))
            except Exception as e:
                warm_results.append({"warm": False, "error": str(e)[:120]})
        rec["warm_results"] = warm_results
        rec["t_warm_done"] = round(time.time() - t0, 1)

    segs = [seg_fn.spawn({"input": {"project_id": BENCH_PID, "phase": "segment_v31", "part": p}})
            for p in range(k)]
    seg_out = []
    ok = True
    for p, c in enumerate(segs):
        try:
            o = c.get(timeout=1700)
        except Exception as e:
            o = {"error": f"segment {p}: {e}"}
        seg_out.append(o)
        if o.get("error"):
            ok = False
    rec["segments"] = seg_out
    rec["t_segments_done"] = round(time.time() - t0, 1)
    if not ok:
        rec["result"] = "SEGMENT_FAIL"
        rec["total_s"] = round(time.time() - t0, 1)
        return rec

    fin = fin_fn.remote({"input": {"project_id": BENCH_PID, "phase": "finish_v31",
                                   "parts": k, "t0": t0,
                                   "tms": {"plan": plan.get("tms"),
                                           "seg": [s.get("tms") for s in seg_out]}}})
    rec["finish"] = fin
    rec["total_s"] = round(time.time() - t0, 1)
    rec["result"] = "OK" if fin.get("ok") else "FINISH_FAIL"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--warm", type=int, default=0, help="사전 예열 GPU 컨테이너 수 (0=예열 없음)")
    ap.add_argument("--label", default="warm")
    ap.add_argument("--plan_on", default="cpu", choices=["cpu", "gpu"])
    ap.add_argument("--out", default="BENCHMARK_V31.json")
    a = ap.parse_args()

    ensure_bench_project()
    records = []
    for i in range(a.runs):
        clean_tmp()
        rid = f"{a.label}-k{a.k}-{i}-{uuid.uuid4().hex[:6]}"
        print(f"\n===== run {i + 1}/{a.runs} ({rid}) =====")
        rec = one_run(a.k, a.warm, rid, plan_on=a.plan_on)
        rec["label"] = a.label
        records.append(rec)
        print("[REC]", json.dumps(rec, ensure_ascii=False, default=str))
        # 실패해도 표본에 포함 (명세 23.2)

    totals = [r["total_s"] for r in records if r.get("result") == "OK"]
    summary = {"label": a.label, "k": a.k, "warm": a.warm, "runs": a.runs,
               "success": len(totals), "fail": a.runs - len(totals)}
    if totals:
        st = sorted(totals)
        summary.update({"min": st[0], "max": st[-1],
                        "p50": round(statistics.median(st), 1),
                        "mean": round(statistics.mean(st), 1),
                        "p95": round(st[max(0, int(len(st) * 0.95) - 1)] if len(st) > 1 else st[-1], 1)})
    print("\n[SUMMARY]", json.dumps(summary, ensure_ascii=False))
    existing = []
    if os.path.exists(a.out):
        try:
            existing = json.load(open(a.out))
        except Exception:
            existing = []
    existing.extend(records)
    json.dump(existing, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(f"[bench] {a.out} 저장 ({len(existing)} records)")
    if summary.get("fail", 0) == a.runs:
        sys.exit(1)


if __name__ == "__main__":
    main()
