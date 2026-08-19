# -*- coding: utf-8 -*-
"""V32 대표 UAT 실행기 (C0→C1 전환 명세 Phase 2/5 — 신규 영상, deep audit 100%).

대표가 서비스에 직접 업로드한 원본 프로젝트(sc_projects)를 UAT 전용 pid로 복제해
V32 staging 파이프라인(scan→segment→finish)으로 처리한다.
 - 원본 행은 절대 수정하지 않음 (복제만) — 운영(v29) 처리와 완전 분리
 - finish는 stream=True + deep_audit=True (전 프레임 전수 검증)
 - 단계별 시간·박스 카운터·검증 결과·다운로드 서명 URL을 UAT_RUN_REPORT로 산출

사용: UAT_SRC_PIDS="<원본pid1>,<원본pid2>" python uat_run_v32.py --label uat1
파이프라인 코드(handler_v32)는 건드리지 않는다 — RC 885a8a9a 고정 원칙.
"""
import argparse, json, os, sys, time

import modal
import requests

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
UAT_PID_BASE = "beac0005-0000-4000-8000-0000000000"   # +NN (UAT 전용 행)
K = 12
KEY_STEP = 5
PREWARM = 3


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def get_project(pid):
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{pid}",
                             "select": "id,user_id,title,source_path,source_bytes,"
                                       "source_duration_sec,probe,status"},
                     headers=sbh(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def clone_project(src_row, uat_pid, idx):
    ins = {"id": uat_pid, "user_id": src_row["user_id"],
           "title": f"[v32-UAT{idx}] " + (src_row.get("title") or ""),
           "source_path": src_row["source_path"],
           "source_bytes": src_row.get("source_bytes"),
           "source_duration_sec": src_row.get("source_duration_sec"),
           "probe": src_row.get("probe"),
           "objective": "wm_remove", "status": "wm_done",
           "status_detail": "v32 staging UAT (검증용 복제본 — 사이트에서 처리하지 마세요)",
           "wm_mode": "auto", "wm_tier": "fast"}
    r = requests.post(f"{SB_URL}/rest/v1/sc_projects",
                      headers=sbh({"Content-Type": "application/json",
                                   "Prefer": "resolution=merge-duplicates,return=minimal"}),
                      data=json.dumps(ins), timeout=30)
    r.raise_for_status()


def clean_tmp(pid):
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": f"wmtmp-v32/{pid}", "limit": 200}),
                      timeout=30)
    if not r.ok:
        return
    names = [f"wmtmp-v32/{pid}/" + o["name"] for o in r.json() if o.get("name")]
    if names:
        requests.request("DELETE", f"{SB_URL}/storage/v1/object/videos-clips",
                         headers=sbh({"Content-Type": "application/json"}),
                         data=json.dumps({"prefixes": names}), timeout=60)


def sign_url(bucket, path, expires=604800):
    r = requests.post(f"{SB_URL}/storage/v1/object/sign/{bucket}/{path}",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"expiresIn": expires}), timeout=30)
    if not r.ok:
        return None
    return SB_URL + "/storage/v1" + r.json().get("signedURL", "")


def run_one(idx, src_spec, rec):
    # src_spec: "<원본pid>" 또는 "<원본pid>@NN" (NN = 복제 pid 접미번호, 충돌 방지)
    if "@" in src_spec:
        src_pid, suffix = src_spec.split("@", 1)
        idx = int(suffix)
    else:
        src_pid = src_spec
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")

    src = get_project(src_pid)
    if not src:
        rec["result"] = "SRC_NOT_FOUND"
        return
    rec["src"] = {"pid": src_pid, "title": src.get("title"),
                  "duration_s": src.get("source_duration_sec"),
                  "bytes": src.get("source_bytes"), "status": src.get("status")}
    uat_pid = f"{UAT_PID_BASE}{idx:02d}"
    rec["uat_pid"] = uat_pid
    clone_project(src, uat_pid, idx)
    clean_tmp(uat_pid)

    t0 = time.time()
    warm_calls = [seg_fn.spawn({"input": {"phase": "warm_v32"}}) for _ in range(PREWARM)]
    scan = scan_fn.remote({"input": {"project_id": uat_pid, "phase": "scan_v32",
                                     "seg_k": K}})
    rec["scan"] = {k: scan.get(k) for k in ("regions", "N", "note", "error", "tms")}
    if scan.get("error") or scan.get("note"):
        rec["result"] = "SCAN_FAIL" if scan.get("error") else "NO_TARGET"
        rec["total_s"] = round(time.time() - t0, 1)
        return
    rec["t_scan_done"] = round(time.time() - t0, 1)

    segs = [seg_fn.spawn({"input": {"project_id": uat_pid, "phase": "segment_v32",
                                    "part": p, "key_step": KEY_STEP}})
            for p in range(K)]
    fin_call = fin_fn.spawn({"input": {"project_id": uat_pid, "phase": "finish_v32",
                                       "parts": K, "t0": t0, "stream": True,
                                       "deep_audit": True}})
    for c in warm_calls:
        try: c.get(timeout=600)
        except Exception: pass

    seg_out, seg_err, counters = [], 0, {}
    gpu_ms = 0
    for c in segs:
        try:
            o = c.get(timeout=1800)
            if o.get("error"):
                seg_err += 1
            gpu_ms += o.get("__exec_ms") or 0
            cc = o.get("counters") or {}
            for k2, v in cc.items():
                if k2.startswith("box") or k2 == "regions_active":
                    counters[k2] = max(counters.get(k2, 0), v) if k2 == "box_conf_max" \
                        else counters.get(k2, 0) + v
            seg_out.append({"part": o.get("part"), "exec_ms": o.get("__exec_ms"),
                            "error": (o.get("error") or "")[:120] or None})
        except Exception as e:
            seg_err += 1
            seg_out.append({"error": str(e)[:120]})
    rec["t_segments_done"] = round(time.time() - t0, 1)
    rec["seg_errors"] = seg_err
    rec["box_counters"] = counters
    rec["segments"] = seg_out

    try:
        fin = fin_call.get(timeout=1800)
    except Exception as e:
        fin = {"error": f"finish: {str(e)[:200]}"}
    rec["finish"] = {k: fin.get(k) for k in ("ok", "sec", "frames", "up_mode",
                                             "out_mb", "tms", "error")}
    rec["total_s"] = round(time.time() - t0, 1)
    ok = bool(fin.get("ok")) and seg_err == 0
    rec["result"] = "OK" if ok else "FAIL"

    # 실측 GPU-s 기반 근사 원가 (L40S $0.000542/s × 부대과금 실측 비율 1.552)
    rec["gpu_s"] = round(gpu_ms / 1000.0, 1)
    rec["cost_est_usd"] = round(gpu_ms / 1000.0 * 0.000542 * 1.552 + 0.02, 3)

    if ok:
        out_path = f"{src['user_id']}/wm_v32_{uat_pid}.mp4"
        rec["download_url"] = sign_url("videos-clips", out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="uat")
    a = ap.parse_args()
    src_pids = [p.strip() for p in os.environ.get("UAT_SRC_PIDS", "").split(",")
                if p.strip()]
    if not src_pids:
        print("UAT_SRC_PIDS 비어 있음 — 원본 프로젝트 id를 지정하라")
        sys.exit(2)
    report = {"label": a.label, "app": APP, "k": K, "key_step": KEY_STEP,
              "prewarm": PREWARM, "deep_audit": True, "runs": []}
    for i, pid in enumerate(src_pids, 1):
        rec = {"idx": i}
        print(f"===== UAT-{i:02d} src={pid} =====")
        try:
            run_one(i, pid, rec)
        except Exception as e:
            rec["result"] = "DRIVER_ERROR"
            rec["error"] = str(e)[:300]
        report["runs"].append(rec)
        print("[UAT]", json.dumps({k: rec.get(k) for k in
              ("idx", "uat_pid", "result", "total_s", "t_scan_done",
               "t_segments_done", "seg_errors", "box_counters", "cost_est_usd")},
              ensure_ascii=False, default=str))
    json.dump(report, open("UAT_RUN_REPORT.json", "w"), ensure_ascii=False,
              indent=1, default=str)

    lines = [f"# V32 대표 UAT 실행 결과 — {a.label}", "",
             "| # | 원본 | 결과 | 총시간(s) | scan(s) | seg오류 | 박스감지 | 원가($) |",
             "|---|---|---|---|---|---|---|---|"]
    for r in report["runs"]:
        src = r.get("src") or {}
        bc = r.get("box_counters") or {}
        box = f"unblend {bc.get('box_unblend', 0)} / ai {bc.get('box_ai', 0)}"
        lines.append(f"| {r['idx']} | {(src.get('title') or '?')[:24]} | "
                     f"{r.get('result')} | {r.get('total_s')} | "
                     f"{r.get('t_scan_done')} | {r.get('seg_errors')} | {box} | "
                     f"{r.get('cost_est_usd')} |")
    lines += ["", "다운로드(7일 서명 URL)는 UAT_RUN_REPORT.json의 download_url 참조."]
    open("UAT_RUN_REPORT.md", "w").write("\n".join(lines) + "\n")
    bad = [r for r in report["runs"] if r.get("result") not in ("OK", "NO_TARGET")]
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
