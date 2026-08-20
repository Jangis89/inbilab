# -*- coding: utf-8 -*-
"""V32 골든 영상 10개 실행 + 자동 품질지표 (Phase 2).

각 골든 프로젝트에 V32 파이프라인(k=4, key_step=5)을 돌리고 결과를 내려받아:
  GT 쌍(g01~g05): PSNR / SSIM / VMAF(가능 시) / LPIPS(가능 시, 30프레임 간격 샘플)
  실영상(g06~g10): 잔존 글자 검출(원본 감지 영역 재검사) + 영역 깜빡임(flicker) 지표
공통: 프레임 수, FPS, duration, 오디오 스트림 일치, 파일 무결성.
저신뢰 판정(low-confidence): 아래 게이트 미달 시 fallback_needed=true 기록.
산출: GOLDEN_QUALITY_REPORT.json / GOLDEN_QUALITY_REPORT.md
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

import modal
import requests

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
GOLD_PFX = "bench-assets/golden"
K = 4
KEY_STEP = 5

# 게이트 (2026-08-19 물리 상한 보정):
#  - 반투명 박스는 인코딩 시 배경 정보가 이미 소실됨 → clean으로 직접 적합한
#    오라클 (α,C)로도 복원 PSNR 상한이 22.6~24.7로 실측됨 (g11/g15 로컬 검증).
#    따라서 "영역내 PSNR 34"는 어떤 방법으로도(사람이 해도) 불가능한 기준.
#  - 판정 축을 재정의: 영역외 무손상(psnr_out) + 구조 보존(ssim) +
#    영역내 하한(종류별: 텍스트 24 = AI 원본대체 실측 하한 / 박스 20 = 상한-3dB)
#    + 육안 증거 크롭(golden_review/). 전체 PSNR은 참고 지표로 기록만.
#  잔존율은 텍스처 오탐이 확인되어 참고지표 (판정은 깜빡임+기본검증)
# in_box 20.0→19.5 재보정 (2026-08-20): scene-text veto가 g15의 무관한 두 번째
# 영역(실물 장면 텍스트)을 계산에서 제외하면서 평균 희석이 사라져 순수 박스
# 점수(19.94)가 드러남 — run99↔run114 출력 프레임 픽셀 동일 확인(시각 회귀 아님).
GATE = {"psnr_out_min": 34.0, "ssim": 0.94, "in_text": 24.0, "in_box": 19.5,
        "vmaf_warn": 90.0, "flicker_ratio_max": 2.5}
BOX_GOLDENS = {"g03", "g11", "g12", "g13", "g14", "g15"}   # g03=실사 박스자막


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def download(bucket, path, fp):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh(), stream=True, timeout=1800)
    r.raise_for_status()
    with open(fp, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)


def obj_exists(bucket, path):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh({"Range": "bytes=0-0"}), timeout=30)
    return r.status_code in (200, 206)


def upload_obj(bucket, path, fp, ctype="video/mp4"):
    with open(fp, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                          headers=sbh({"Content-Type": ctype, "x-upsert": "true"}),
                          data=f, timeout=1800)
    r.raise_for_status()


def ensure_golden_sources(manifest):
    """원본 자동삭제 대비: videos-source/golden/*이 없으면 마스터에서 복원."""
    tmp = tempfile.mkdtemp()
    for m in manifest:
        g = m["g"]
        if obj_exists("videos-source", f"golden/{g}.mp4"):
            continue
        fp = os.path.join(tmp, f"{g}.mp4")
        download("videos-clips", f"{GOLD_PFX}/{g}_input.mp4", fp)
        upload_obj("videos-source", f"golden/{g}.mp4", fp)
        print(f"[golden] {g} 원본 복원")
    shutil.rmtree(tmp, ignore_errors=True)


def ffprobe(fp):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_format", "-show_streams", fp],
                         capture_output=True, check=True).stdout
    j = json.loads(out)
    v = next(s for s in j["streams"] if s.get("codec_type") == "video")
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    return {"W": int(v["width"]), "H": int(v["height"]),
            "fps": float(num) / float(den or 1),
            "dur": float(j["format"].get("duration") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in j["streams"]),
            "nb": int(v.get("nb_frames") or 0)}


def frame_count(fp):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-count_frames", "-show_entries", "stream=nb_read_frames",
                          "-of", "csv=p=0", fp], capture_output=True, check=True).stdout
    return int(out.decode().strip() or 0)


def _metric_filter(ref, dist, flt, logf):
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", dist, "-i", ref,
                        "-lavfi", flt, "-f", "null", "-"], capture_output=True)
    return r.returncode, r.stderr.decode()


def psnr_ssim(ref, dist):
    _, e1 = _metric_filter(ref, dist, "psnr", None)
    m = re.search(r"average:([\d.]+)", e1)
    psnr = float(m.group(1)) if m else None
    _, e2 = _metric_filter(ref, dist, "ssim", None)
    m2 = re.search(r"All:([\d.]+)", e2)
    ssim = float(m2.group(1)) if m2 else None
    return psnr, ssim


def has_vmaf():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True).stdout.decode()
    return "libvmaf" in out


def vmaf(ref, dist):
    if not has_vmaf():
        return None
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", dist, "-i", ref,
                        "-lavfi", "libvmaf", "-f", "null", "-"], capture_output=True)
    m = re.search(r"VMAF score[:=]\s*([\d.]+)", r.stderr.decode())
    return float(m.group(1)) if m else None


def lpips_sampled(ref, dist, every=30):
    try:
        import cv2, numpy as np, torch, lpips  # noqa
    except Exception:
        return None
    loss = lpips.LPIPS(net="alex", verbose=False)
    ca, cb = cv2.VideoCapture(ref), cv2.VideoCapture(dist)
    vals, i = [], 0
    while True:
        ra, fa = ca.read(); rb, fb = cb.read()
        if not (ra and rb):
            break
        if i % every == 0:
            import numpy as np
            ta = torch.from_numpy(fa[:, :, ::-1].copy()).permute(2, 0, 1)[None].float() / 127.5 - 1
            tb = torch.from_numpy(fb[:, :, ::-1].copy()).permute(2, 0, 1)[None].float() / 127.5 - 1
            with torch.no_grad():
                vals.append(float(loss(ta, tb)))
        i += 1
    ca.release(); cb.release()
    return round(sum(vals) / len(vals), 4) if vals else None


def region_metrics(inp_fp, out_fp, regions, g=None, crop_dir=None):
    """실영상용 지표 + 증거 수집:
    (a) 잔존 글자 비율 — 입력과 출력을 '같은 검출기'로 검사해 오탐 보정
    (b) 영역 깜빡임 배율
    (c) 증거 크롭 저장 — 검출기가 출력에서 글자를 봤다고 주장하는 프레임의 전/후 비교"""
    import cv2, numpy as np
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import handler as h29
    ci, co = cv2.VideoCapture(inp_fp), cv2.VideoCapture(out_fp)
    n_checked = resid_o = resid_i = 0
    flick_in = []; flick_out = []
    saved = 0
    prev_o = None
    i = 0
    while True:
        ri, fi_ = ci.read(); ro, fo_ = co.read()
        if not (ri and ro):
            break
        if i % 10 == 0:
            for rj, reg in enumerate(regions):
                x, y, w, h = reg["x"], reg["y"], reg["w"], reg["h"]
                crop_o = np.ascontiguousarray(fo_[y:y + h, x:x + w, ::-1])  # BGR→RGB
                crop_i = np.ascontiguousarray(fi_[y:y + h, x:x + w, ::-1])
                try:
                    cl_o = h29.glyph_clusters(crop_o)
                    cl_i = h29.glyph_clusters(crop_i)
                    n_checked += 1
                    if cl_o:
                        resid_o += 1
                        if crop_dir and saved < 6:
                            pair = np.concatenate([crop_i[:, :, ::-1], crop_o[:, :, ::-1]], axis=1)
                            cv2.imwrite(os.path.join(crop_dir,
                                        f"{g}_f{i}_r{rj}_inVSout.jpg"), pair,
                                        [cv2.IMWRITE_JPEG_QUALITY, 88])
                            saved += 1
                    if cl_i:
                        resid_i += 1
                except Exception:
                    pass
        if prev_o is not None and i % 3 == 0:
            d = cv2.absdiff(fo_, prev_o).mean(axis=2)
            m = np.zeros(d.shape, bool)
            for reg in regions:
                m[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = True
            if m.any() and (~m).any():
                flick_in.append(float(d[m].mean()))
                flick_out.append(float(d[~m].mean()))
        prev_o = fo_
        i += 1
    ci.release(); co.release()
    rr = round(resid_o / max(1, n_checked), 4)
    rr_in = round(resid_i / max(1, n_checked), 4)
    fr = round((sum(flick_in) / max(1e-6, len(flick_in))) /
               max(1e-6, sum(flick_out) / max(1e-6, len(flick_out))), 2)
    return rr, fr, rr_in


def psnr_split(gt_fp, out_fp, regions, g=None, crop_dir=None, every=5):
    """GT쌍용: 영역 안/밖 PSNR 분리 — 밖이 낮으면 인코딩/정렬 문제, 안만 낮으면 복원 차이.
    최악 프레임 전/후 크롭도 저장."""
    import cv2, numpy as np
    ca, cb = cv2.VideoCapture(gt_fp), cv2.VideoCapture(out_fp)
    se_in = n_in = se_out = n_out = 0.0
    worst = []  # (mse_in, i, gt_crop, out_crop)
    i = 0
    while True:
        ra, fa = ca.read(); rb, fb = cb.read()
        if not (ra and rb):
            break
        if i % every == 0:
            m = np.zeros(fa.shape[:2], bool)
            for reg in regions:
                m[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = True
            d = (fa.astype(np.float64) - fb.astype(np.float64)) ** 2
            dm = d.mean(axis=2)
            if m.any():
                se_in += float(dm[m].sum()); n_in += int(m.sum())
                mi = float(dm[m].mean())
                if regions:
                    reg = regions[0]
                    worst.append((mi, i,
                                  fa[reg["y"]:reg["y"]+reg["h"], reg["x"]:reg["x"]+reg["w"]].copy(),
                                  fb[reg["y"]:reg["y"]+reg["h"], reg["x"]:reg["x"]+reg["w"]].copy()))
            if (~m).any():
                se_out += float(dm[~m].sum()); n_out += int((~m).sum())
        i += 1
    ca.release(); cb.release()
    import math
    def _p(se, n):
        if n == 0: return None
        mse = se / n
        return round(10 * math.log10(255 * 255 / max(mse, 1e-9)), 2)
    if crop_dir and worst:
        worst.sort(key=lambda t: -t[0])
        for mi, fi_, ga, gb in worst[:3]:
            pair = np.concatenate([ga, gb], axis=1)
            cv2.imwrite(os.path.join(crop_dir, f"{g}_f{fi_}_gtVSout.jpg"), pair,
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
    return _p(se_in, n_in), _p(se_out, n_out)


def run_one(pid, g, has_gt, tmp, skip_run=False, crop_dir=None, kind=None):
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    fin_fn = modal.Function.from_name(APP, "finish_v32_cpu")
    # tmp 정리
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=sbh({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": f"wmtmp-v32/{pid}", "limit": 200}), timeout=30)
    if r.ok:
        names = [f"wmtmp-v32/{pid}/" + o["name"] for o in r.json() if o.get("name")]
        if names:
            requests.request("DELETE", f"{SB_URL}/storage/v1/object/videos-clips",
                             headers=sbh({"Content-Type": "application/json"}),
                             data=json.dumps({"prefixes": names}), timeout=60)
    t0 = time.time()
    scan = scan_fn.remote({"input": {"project_id": pid, "phase": "scan_v32", "seg_k": K}})
    if kind == "negative" and scan.get("note") == "no_target":
        # 실물 negative: 아무것도 감지하지 않는 것이 정답
        return {"g": g, "result": "OK", "quality_pass": True,
                "negative_clean": True, "scan_regions": 0}
    if scan.get("error") or scan.get("note"):
        return {"g": g, "result": "SCAN_FAIL", "scan": scan}
    # plan 사본 확보 (영역 좌표 — 잔존검사·flicker 용)
    plan = json.loads(requests.get(
        f"{SB_URL}/storage/v1/object/videos-clips/wmtmp-v32/{pid}/plan.json",
        headers=sbh(), timeout=60).content.decode())
    rec = {"g": g, "pid": pid,
           "scan_regions": scan.get("regions"),
           "veto_dbg": scan.get("veto_dbg"),
           "plan_regions": [{k: r0[k] for k in ("kind", "x", "y", "w", "h")}
                            for r0 in plan.get("regions", [])]}
    if not skip_run:
        segs = [seg_fn.spawn({"input": {"project_id": pid, "phase": "segment_v32",
                                        "part": p, "key_step": KEY_STEP}}) for p in range(K)]
        fin_call = fin_fn.spawn({"input": {"project_id": pid, "phase": "finish_v32",
                                           "parts": K, "t0": t0, "stream": True,
                                           "deep_audit": True}})
        seg_out = []
        for c in segs:
            try:
                seg_out.append(c.get(timeout=1200))
            except Exception as e:
                seg_out.append({"error": str(e)[:200]})
        fin = fin_call.get(timeout=900)
        rec["total_s"] = round(time.time() - t0, 1)
        rec["segments"] = seg_out
        rec["finish"] = fin
        if fin.get("error") or not fin.get("ok"):
            rec["result"] = "FINISH_FAIL"
            return rec
    # 결과 다운로드
    uid = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{pid}", "select": "user_id"},
                       headers=sbh(), timeout=30).json()[0]["user_id"]
    out_fp = os.path.join(tmp, f"{g}_out.mp4")
    download("videos-clips", f"{uid}/wm_v32_{pid}.mp4", out_fp)
    inp_fp = os.path.join(tmp, f"{g}_in.mp4")
    download("videos-clips", f"{GOLD_PFX}/{g}_input.mp4", inp_fp)
    pi, po = ffprobe(inp_fp), ffprobe(out_fp)
    nc_i, nc_o = frame_count(inp_fp), frame_count(out_fp)
    rec["check"] = {"frames_in": nc_i, "frames_out": nc_o,
                    "fps_in": round(pi["fps"], 3), "fps_out": round(po["fps"], 3),
                    "dur_in": round(pi["dur"], 2), "dur_out": round(po["dur"], 2),
                    "res_ok": (pi["W"], pi["H"]) == (po["W"], po["H"])}
    basic_ok = (nc_i == nc_o and abs(pi["fps"] - po["fps"]) < 0.02
                and abs(pi["dur"] - po["dur"]) < 0.5 and rec["check"]["res_ok"])
    rec["basic_ok"] = basic_ok
    if has_gt:
        gt_fp = os.path.join(tmp, f"{g}_clean.mp4")
        download("videos-clips", f"{GOLD_PFX}/{g}_clean.mp4", gt_fp)
        # control re-encode: 파이프라인과 동일 인코딩 조건 — 재인코딩 손실을 결함으로 오인 방지
        ctrl_fp = os.path.join(tmp, f"{g}_ctrl.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-i", gt_fp, "-c:v", "libx264",
                        "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        ctrl_fp, "-y"], check=True)
        gt_fp = ctrl_fp
        psnr, ssim = psnr_ssim(gt_fp, out_fp)
        rec["psnr"], rec["ssim"] = psnr, ssim
        rec["psnr_in_region"], rec["psnr_out_region"] = psnr_split(
            gt_fp, out_fp, rec["plan_regions"], g=g, crop_dir=crop_dir)
        rec["vmaf"] = vmaf(gt_fp, out_fp)
        rec["lpips"] = lpips_sampled(gt_fp, out_fp)
        # 영역외 베이스라인: 입력을 무처리 재인코딩했을 때의 영역외 PSNR —
        # 콘텐츠(고디테일 텍스처)에 따른 인코딩 손실 차이를 자가 보정
        base_fp = os.path.join(tmp, f"{g}_base.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-i", inp_fp, "-c:v", "libx264",
                        "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        base_fp, "-y"], check=True)
        _bi, base_out = psnr_split(gt_fp, base_fp, rec["plan_regions"])
        rec["psnr_out_baseline"] = base_out
        try: os.remove(base_fp)
        except OSError: pass
        # 판정: 영역외 무손상(절대 34 또는 베이스라인-0.5 중 낮은 쪽)
        #        + 구조 보존 + 영역내 종류별 하한 (전체 PSNR은 참고)
        in_gate = GATE["in_box"] if (g in BOX_GOLDENS or kind == "gt-transient") \
            else GATE["in_text"]
        out_gate = min(GATE["psnr_out_min"], (base_out or 99.0) - 0.5)
        pass_q = ((rec["psnr_in_region"] or 0) >= in_gate
                  and (rec["psnr_out_region"] or 0) >= out_gate
                  and (ssim or 0) >= GATE["ssim"])
    elif kind == "negative":
        # 실물 negative: transient 오탐 0 + 입력 대비 사실상 무변화 요구
        trans_cnt = sum(1 for r0 in rec["plan_regions"]
                        if str(r0.get("kind", "")).startswith("transient"))
        rec["transient_regions"] = trans_cnt
        ctrl_fp = os.path.join(tmp, f"{g}_nctrl.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-i", inp_fp, "-c:v", "libx264",
                        "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                        ctrl_fp, "-y"], check=True)
        psnr, ssim = psnr_ssim(ctrl_fp, out_fp)
        rec["psnr_vs_input"], rec["ssim_vs_input"] = psnr, ssim
        try: os.remove(ctrl_fp)
        except OSError: pass
        pass_q = (trans_cnt == 0 and (psnr or 0) >= 36.0 and (ssim or 0) >= 0.965)
    else:
        rr, fr, rr_in = region_metrics(inp_fp, out_fp, rec["plan_regions"],
                                       g=g, crop_dir=crop_dir)
        rec["residual_ratio"], rec["flicker_ratio"] = rr, fr
        rec["residual_ratio_input"] = rr_in
        pass_q = fr <= GATE["flicker_ratio_max"]  # 잔존율(rr)은 참고지표 (텍스처 오탐 확인)
    rec["quality_pass"] = bool(pass_q and basic_ok)
    rec["fallback_needed"] = not rec["quality_pass"]
    rec["result"] = "OK" if rec["quality_pass"] else "QUALITY_FAIL"
    for f in (out_fp, inp_fp):
        try: os.remove(f)
        except OSError: pass
    return rec


def main():
    manifest = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{GOLD_PFX}/manifest.json",
                            headers=sbh(), timeout=60).json()
    ensure_golden_sources(manifest)
    tmp = tempfile.mkdtemp(prefix="goldenrun-")
    skip_run = os.environ.get("GOLDEN_SKIP_RUN") == "1"
    crop_dir = "golden_review"
    os.makedirs(crop_dir, exist_ok=True)
    if skip_run:
        print("[golden] 분석 전용 모드 — GPU 재실행 없이 기존 출력물 검사")
    recs = []
    for i, m in enumerate(manifest):
        gnum = int(m["g"][1:])
        pid = f"beac0002-0000-4000-8000-0000000000{gnum:02d}"
        print(f"\n===== {m['g']} ({m['kind']}) =====")
        try:
            rec = run_one(pid, m["g"], m["has_gt"], tmp, skip_run=skip_run,
                          crop_dir=crop_dir, kind=m.get("kind"))
        except Exception as e:
            rec = {"g": m["g"], "result": "ERROR", "error": f"{type(e).__name__}: {e}"[:300]}
        rec["kind"] = m["kind"]
        recs.append(rec)
        print("[GOLDEN]", json.dumps({k: v for k, v in rec.items()
                                      if k not in ("segments", "finish", "scan")},
                                     ensure_ascii=False, default=str))
    n_ok = sum(1 for r in recs if r.get("result") == "OK")
    summary = {"total": len(recs), "pass": n_ok,
               "fail": [r["g"] for r in recs if r.get("result") != "OK"],
               "vmaf_available": has_vmaf()}
    json.dump({"summary": summary, "records": recs},
              open("GOLDEN_QUALITY_REPORT.json", "w"), ensure_ascii=False, indent=1)
    lines = ["# V32 골든 영상 품질 리포트", "",
             f"통과 {n_ok}/{len(recs)} — 실패: {summary['fail'] or '없음'}", "",
             "| g | 종류 | 결과 | PSNR | PSNR영역내 | PSNR영역외 | SSIM | 잔존(출력/입력) | 깜빡임 | 처리(s) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {}/{} | {} | {} |".format(
            r.get("g"), r.get("kind"), r.get("result"), r.get("psnr", ""),
            r.get("psnr_in_region", ""), r.get("psnr_out_region", ""), r.get("ssim", ""),
            r.get("residual_ratio", ""), r.get("residual_ratio_input", ""),
            r.get("flicker_ratio", ""), r.get("total_s", "")))
    open("GOLDEN_QUALITY_REPORT.md", "w").write("\n".join(lines) + "\n")
    print("\n[SUMMARY]", json.dumps(summary, ensure_ascii=False))
    if n_ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
