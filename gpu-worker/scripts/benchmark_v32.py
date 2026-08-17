# -*- coding: utf-8 -*-
"""V32 스테이징 벤치마크 — 스트리밍 파이프라인 (scan → segment 즉시 분산 → finish).

warm 예열은 scan과 '동시에' 시작한다 (scan이 짧아져 겹치기가 유효해짐 — V32 설계).
사용: python scripts/benchmark_v32.py --runs 3 --k 10 --warm 5 --key_step 5 --label smoke
"""
import argparse, json, os, statistics, sys, time, uuid

import modal
import requests

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]

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
           "objective": "wm_remove", "status": "wm_queued", "status_detail": "v32 bench",
           "wm_mode": "auto", "wm_tier": "fast"}
    r2 = requests.post(f"{SB_URL}/rest/v1/sc_projects",
                       headers=sbh({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                       data=json.dumps(ins), timeout=30)
    r2.raise_for_status()


def clean_tmp():
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": f"wmtmp-v32/{BENCH_PID}", "limit": 200}), timeout=30)
    if not r.ok:
        return
    names = [f"wmtmp-v32/{BENCH_PID}/" + o["name"] for o in r.json() if o.get("name")]
    if names:
        requests.request("DELETE", f"{SB_URL}/storage/v1/object/videos-clips",
                         headers=sbh({"Content-Type": "application/json"}),
                         data=json.dumps({"prefixes": names}), timeout=60)


def one_run(k, warm_n, key_step, run_id):
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")

    rec = {"run_id": run_id, "k": k, "warm_req": warm_n, "key_step": key_step,
           "app": APP, "t_start": time.time()}
    t0 = time.time()

    # scan과 GPU 예열 동시 시작 (scan이 짧아 겹침이 유효)
    warm_calls = [seg_fn.spawn({"input": {"phase": "warm_v32"}}) for _ in range(warm_n)]
    scan = scan_fn.remote({"input": {"project_id": BENCH_PID, "phase": "scan_v32", "seg_k": k}})
    rec["scan"] = scan
    if scan.get("error") or scan.get("note"):
        rec["result"] = "SCAN_FAIL"
        rec["total_s"] = round(time.time() - t0, 1)
        return rec
    rec["t_scan_done"] = round(time.time() - t0, 1)

    # segment 즉시 분산 (전체 plan 대기 없음 — 마스크는 각 worker가 스스로 생성)
    segs = [seg_fn.spawn({"input": {"project_id": BENCH_PID, "phase": "segment_v32",
                                    "part": p, "key_step": key_step}})
            for p in range(k)]
    rec["t_first_dispatch"] = round(time.time() - t0, 1)
    warm_results = []
    for c in warm_calls:
        try:
            warm_results.append(c.get(timeout=600))
        except Exception as e:
            warm_results.append({"warm": False, "error": str(e)[:120]})
    rec["warm_results"] = warm_results
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

    fin = fin_fn.remote({"input": {"project_id": BENCH_PID, "phase": "finish_v32",
                                   "parts": k, "t0": t0,
                                   "tms": {"scan": scan.get("tms"),
                                           "seg": [s.get("tms") for s in seg_out]}}})
    rec["finish"] = fin
    rec["total_s"] = round(time.time() - t0, 1)
    rec["result"] = "OK" if fin.get("ok") else "FINISH_FAIL"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--warm", type=int, default=5)
    ap.add_argument("--key_step", type=int, default=5)
    ap.add_argument("--label", default="v32")
    ap.add_argument("--out", default="BENCHMARK_V32.json")
    a = ap.parse_args()

    ensure_bench_project()
    records = []
    for i in range(a.runs):
        clean_tmp()
        rid = f"{a.label}-k{a.k}-ks{a.key_step}-{i}-{uuid.uuid4().hex[:6]}"
        print(f"\n===== run {i + 1}/{a.runs} ({rid}) =====")
        rec = one_run(a.k, a.warm, a.key_step, rid)
        rec["label"] = a.label
        records.append(rec)
        print("[REC]", json.dumps(rec, ensure_ascii=False, default=str))

    totals = sorted(r["total_s"] for r in records if r.get("result") == "OK")
    summary = {"label": a.label, "k": a.k, "warm": a.warm, "key_step": a.key_step,
               "runs": a.runs, "success": len(totals), "fail": a.runs - len(totals)}
    if totals:
        summary.update({"min": totals[0], "max": totals[-1],
                        "p50": round(statistics.median(totals), 1),
                        "mean": round(statistics.mean(totals), 1),
                        "p95": round(totals[max(0, int(len(totals) * 0.95) - 1)]
                                     if len(totals) > 1 else totals[-1], 1)})
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
