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


SRC_PATH = "4117e902-3396-4b14-aea0-957b326ab563/1786760197372_s94d16lfj6.mp4"
MASTER_PATH = "bench-assets/benchmark_master.mp4"          # videos-clips (장기 보관용)
AUDIT_ZIP = "4117e902-3396-4b14-aea0-957b326ab563/audit/SUBTITLE_REMOVER_MEDIA_AUDIT_PART1.zip"


def _obj_exists(bucket, path):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh({"Range": "bytes=0-0"}), timeout=30)
    return r.status_code in (200, 206)


def _upload_obj(bucket, path, fp, ctype="video/mp4"):
    with open(fp, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                          headers=sbh({"Content-Type": ctype, "x-upsert": "true"}),
                          data=f, timeout=1800)
    r.raise_for_status()


def ensure_source():
    """운영의 원본 자동삭제 정책으로 벤치 원본이 사라질 수 있음 → 벤치 스스로 복원.
    우선순위: 이미 있음 → videos-clips 마스터에서 복원 → 감사 PART1.zip에서 추출·복원."""
    if _obj_exists("videos-source", SRC_PATH):
        return
    print("[bench] 기준 원본 없음 — 자가복구 시작")
    import tempfile, zipfile, shutil as _sh
    tmp = tempfile.mkdtemp()
    fp = os.path.join(tmp, "master.mp4")
    if _obj_exists("videos-clips", MASTER_PATH):
        r = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{MASTER_PATH}",
                         headers=sbh(), stream=True, timeout=900)
        r.raise_for_status()
        with open(fp, "wb") as f:
            for ch in r.iter_content(1 << 20):
                f.write(ch)
        print(f"[bench] 마스터 사본에서 복원 ({os.path.getsize(fp)/1e6:.1f}MB)")
    else:
        zp = os.path.join(tmp, "audit.zip")
        r = requests.get(f"{SB_URL}/storage/v1/object/videos-source/{AUDIT_ZIP}",
                         headers=sbh(), stream=True, timeout=1800)
        r.raise_for_status()
        with open(zp, "wb") as f:
            for ch in r.iter_content(1 << 20):
                f.write(ch)
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            cand = [n for n in names if "1786760197372" in n] or \
                   [n for n in names if n.lower().endswith(".mp4") and
                    ("input" in n.lower() or "original" in n.lower() or "원본" in n)] or \
                   sorted((n for n in names if n.lower().endswith(".mp4")),
                          key=lambda n: abs(z.getinfo(n).file_size - 102_600_000))
            if not cand:
                raise RuntimeError("감사 zip에서 원본 mp4를 찾지 못함: " + ",".join(names[:20]))
            m = cand[0]
            print(f"[bench] 감사 zip에서 추출: {m} ({z.getinfo(m).file_size/1e6:.1f}MB)")
            with z.open(m) as srcf, open(fp, "wb") as dst:
                _sh.copyfileobj(srcf, dst, 1 << 20)
        _upload_obj("videos-clips", MASTER_PATH, fp)
        print("[bench] 마스터 사본 저장(videos-clips/bench-assets)")
    _upload_obj("videos-source", SRC_PATH, fp)
    if not _obj_exists("videos-source", SRC_PATH):
        raise RuntimeError("원본 복원 실패")
    print("[bench] 기준 원본 복원 완료 ✅")


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


def one_run(k, warm_n, key_step, run_id, hedge_s=0):
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
    segs = []
    dispatch_at = []
    for p in range(k):
        dispatch_at.append(time.time())
        segs.append(seg_fn.spawn({"input": {"project_id": BENCH_PID, "phase": "segment_v32",
                                            "part": p, "key_step": key_step}}))
    rec["t_first_dispatch"] = round(time.time() - t0, 1)
    # 스트리밍 마무리: 세그 도착을 finish node가 폴링하며 즉시 다운로드·검증 (Phase 1)
    fin_call = fin_fn.spawn({"input": {"project_id": BENCH_PID, "phase": "finish_v32",
                                       "parts": k, "t0": t0, "stream": True}})
    warm_results = []
    for c in warm_calls:
        try:
            warm_results.append(c.get(timeout=600))
        except Exception as e:
            warm_results.append({"warm": False, "error": str(e)[:120]})
    rec["warm_results"] = warm_results
    # 완료 폴링 — 세그별 done 절대시각 기록 (worker 배정·실행 분리 계측)
    # hedge_s>0: 발사 후 hedge_s 지나도 미완료인 세그는 복제 발사, 먼저 온 유효 결과 채택
    seg_out = [None] * k
    done_at = [None] * k
    hedge_calls = {}
    hedge_used = []
    deadline = time.time() + 1700
    pending = set(range(k))
    while pending and time.time() < deadline:
        for p in list(pending):
            src_calls = [segs[p]] + ([hedge_calls[p]] if p in hedge_calls else [])
            got = None
            for ci, call in enumerate(src_calls):
                try:
                    o = call.get(timeout=0)
                except TimeoutError:
                    continue
                except Exception as e:
                    o = {"error": f"segment {p}: {e}"}
                if o.get("error") and ci == 0 and p in hedge_calls:
                    continue  # 원본 실패 — hedge 결과를 기다림
                got = o
                if ci == 1:
                    o["hedge_winner"] = True
                break
            if got is None:
                if hedge_s and p not in hedge_calls                         and time.time() - dispatch_at[p] > hedge_s:
                    hedge_calls[p] = seg_fn.spawn(
                        {"input": {"project_id": BENCH_PID, "phase": "segment_v32",
                                   "part": p, "key_step": key_step}})
                    hedge_used.append(p)
                continue
            seg_out[p] = got
            done_at[p] = time.time()
            pending.discard(p)
        if pending:
            time.sleep(2)
    rec["hedged_parts"] = hedge_used
    ok = True
    for p in range(k):
        if seg_out[p] is None:
            seg_out[p] = {"error": f"segment {p}: poll timeout"}
        o = seg_out[p]
        if o.get("error"):
            ok = False
        elif o.get("t_enter"):
            o["alloc_wait_s"] = round(o["t_enter"] - dispatch_at[p], 1)
            o["exec_wall_s"] = round((o.get("t_done") or done_at[p]) - o["t_enter"], 1)
    rec["seg_dispatch_at"] = [round(d - t0, 1) for d in dispatch_at]
    rec["seg_done_at"] = [round(d - t0, 1) if d else None for d in done_at]
    rec["segments"] = seg_out
    rec["t_segments_done"] = round(time.time() - t0, 1)
    if not ok:
        rec["result"] = "SEGMENT_FAIL"
        rec["total_s"] = round(time.time() - t0, 1)
        return rec

    try:
        fin = fin_call.get(timeout=600)
    except Exception as e:
        fin = {"error": f"finish: {e}"}
    rec["finish"] = fin
    rec["total_s"] = round(time.time() - t0, 1)
    rec["result"] = "OK" if fin.get("ok") else "FINISH_FAIL"
    return rec


def upbench_run(label, out_path):
    fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")
    out = fin_fn.remote({"input": {"project_id": BENCH_PID, "phase": "upbench_v32"}})
    out["label"] = label
    print("[UPBENCH]", json.dumps(out, ensure_ascii=False))
    json.dump([out], open(out_path, "w"), ensure_ascii=False, indent=1)
    if out.get("error"):
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--warm", type=int, default=5)
    ap.add_argument("--key_step", type=int, default=5)
    ap.add_argument("--label", default="v32")
    ap.add_argument("--hedge", type=int, default=0)
    ap.add_argument("--out", default="BENCHMARK_V32.json")
    a = ap.parse_args()

    ensure_source()
    ensure_bench_project()
    if a.label.startswith("upbench"):
        upbench_run(a.label, a.out)
        return
    records = []
    for i in range(a.runs):
        clean_tmp()
        rid = f"{a.label}-k{a.k}-ks{a.key_step}-{i}-{uuid.uuid4().hex[:6]}"
        print(f"\n===== run {i + 1}/{a.runs} ({rid}) =====")
        rec = one_run(a.k, a.warm, a.key_step, rid, hedge_s=a.hedge)
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
