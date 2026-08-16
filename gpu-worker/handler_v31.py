# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 자막·워터마크 제거기 — V31 파이프라인 (스테이징)
# 설계 원칙:
#   1) v29(handler.py)의 감지·마스크·AI·합성 함수를 "그대로 재사용" → bitwise 동일성 보장
#   2) CPU plan / GPU segment / CPU finish 자원 분리 (modal_app_v31.py)
#   3) 한 GPU 워커가 자기 시간구간을 정확 해독 → AI 복원 → 즉시 합성 → 세그먼트 1회 인코딩
#   4) 중간 AI 조각 MP4(o_*.mp4) 생성 금지
#   5) 임시 저장 prefix wmtmp-v31/{pid}/ — 운영(wmtmp/)과 완전 분리
# ============================================================
import os, json, time, subprocess, tempfile, shutil, traceback
import numpy as np

import handler as h29  # v29 함수 재사용 (같은 디렉터리)

V31_VER = "v31"
PFX = "wmtmp-v31"
BACKEND_NAME = os.environ.get("WM_BACKEND_NAME", "modal-v31")

cv2 = h29.cv2
SW = h29.SW


# ---------------- v31 임시 저장 (prefix만 다름) ----------------
def tmp_upload(pid, name, data, ctype="application/octet-stream"):
    h29.tmp_upload(f"{PFX}/{pid}/{name}", data, ctype)

def tmp_download(pid, name):
    return h29.tmp_download(f"{PFX}/{pid}/{name}")

def tmp_delete(pid, names):
    h29.tmp_delete(f"{PFX}/{pid}", names)


def fetch_lite_v31(proj, tmp, plan):
    """v29 fetch_lite와 동일하되 cfr work를 v31 prefix에서 받는다"""
    pid = proj["id"]
    src = h29.cache_get(pid + "-src.mp4")
    if not src:
        src = os.path.join(tmp, "src.mp4")
        h29.download_to(h29.signed_url(proj["source_path"], 21600), src)
        h29.cache_put(pid + "-src.mp4", src)
    work = src
    if plan.get("cfr"):
        work = h29.cache_get(pid + "-work31.mp4")
        if not work:
            work = os.path.join(tmp, "work.mp4")
            with open(work, "wb") as f:
                f.write(tmp_download(pid, "work.mp4"))
            h29.cache_put(pid + "-work31.mp4", work)
    return src, work


# ---------------- 정확한 구간 해독 (exact range decode) ----------------
# CFR 전제(plan에서 보장: 어긋나면 fps 정규화 work 생성됨).
# -ss는 -i 앞(입력 시킹): 이전 키프레임으로 이동 후 목표 시각까지 해독·폐기 → 프레임 정확.
# 정확성은 test_equivalence(같은 프레임 byte 동일)로 게이트한다.

def _range_args(f0, fps):
    if f0 <= 0:
        return []
    # 반 프레임 앞 시각으로 시킹해 반올림 경계에서 프레임이 밀리지 않게 한다
    t = (f0 - 0.5) / fps
    return ["-ss", f"{t:.6f}"]

def stream_frames_range(path, W, H, f0, f1, fps, hw=True):
    """[f0, f1) 전체 해상도 RGB 프레임 발생기. 반환 개수 == f1-f0 을 호출측에서 assert."""
    n_need = f1 - f0
    cmd = (["ffmpeg", "-v", "error"] + (h29.hw_dec_args() if hw else [])
           + _range_args(f0, fps) + ["-i", path,
           "-vframes", str(n_need), "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fsz = W * H * 3
    n = 0
    try:
        while n < n_need:
            buf = p.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            n += 1
    finally:
        try: p.stdout.close()
        except Exception: pass
        p.wait()
    if n != n_need:
        raise RuntimeError(f"range decode 프레임 수 불일치: 기대 {n_need}, 실제 {n} (f0={f0}, f1={f1})")

def read_crop_range(path, x, y, w, h, f0, f1, fps, hw=True):
    """[f0, f1) 구간을 v29와 같은 필터(format=rgb24,crop)로 잘라 목록으로 반환."""
    n_need = f1 - f0
    cmd = (["ffmpeg", "-v", "error"] + (h29.hw_dec_args() if hw else [])
           + _range_args(f0, fps) + ["-i", path,
           "-vf", f"format=rgb24,crop={w}:{h}:{x}:{y}",
           "-vframes", str(n_need), "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fsz = w * h * 3
    out = []
    try:
        while len(out) < n_need:
            buf = p.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            out.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
    finally:
        try: p.stdout.close()
        except Exception: pass
        p.wait()
    if len(out) != n_need:
        raise RuntimeError(f"crop range decode 프레임 수 불일치: 기대 {n_need}, 실제 {len(out)} (f0={f0})")
    return out


# ---------------- 단계: plan (CPU) ----------------
def plan_v31(proj, tmp, scan_step=12, seg_k=5):
    """v29 phase_plan과 동일 알고리즘·동일 산출(감지/마스크/조각) + v31 세그먼트 계획.
    저장 위치만 wmtmp-v31. 감지·마스크 결과는 v29와 완전 동일해야 한다."""
    pid = proj["id"]
    sw = SW()
    h29.set_proj(pid, "wm_running", "[v31] 영상을 받아 오는 중…")
    src, work, info, N = h29.fetch_source(proj, tmp)
    sw.mark("dl_cnt")
    cfr = (work != src)
    if cfr:
        with open(work, "rb") as f:
            tmp_upload(pid, "work.mp4", f.read(), "video/mp4")
        sw.mark("cfr_up")
    W, H = info["W"], info["H"]
    mode = "manual" if proj.get("wm_mode") == "manual" else "auto"
    regions = []
    if mode == "manual":
        regions = h29.detect_regions(proj, work, info, N, "manual")
    else:
        h29.set_proj(pid, "wm_running", "[v31] 자막·워터마크를 찾는 중…")
        samples = list(h29.stream_frames(work, W, H, sample_every=scan_step))
        sw.mark("scan_dec")
        regions.extend(h29.detect_sub_bands_from(samples, W, H))
        for side in ("tl", "tr"):
            c = h29.detect_corner_from(samples, W, H, side)
            if c: regions.append(c)
        del samples
        sw.mark("scan")
    if not regions:
        h29.set_proj(pid, "wm_done", {"note": "no_target", "ver": V31_VER,
            "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
        return {"note": "no_target"}
    h29.set_proj(pid, "wm_running", "[v31] 자막 글자 위치를 정밀하게 잡는 중…")
    crops = h29.read_all_crops(work, W, H, regions)
    if crops and crops.get(0) is not None and len(crops[0]) and len(crops[0]) != N:
        N = len(crops[0])
    sw.mark("mask_dec")
    plan_regions = []
    all_chunks = []
    for ri, reg in enumerate(regions):
        frames = crops[ri]
        n = len(frames)
        if reg["kind"].startswith(("subtitle", "label")):
            masks, masked = h29.build_subtitle_masks(frames, n, with_labels=not reg["kind"].startswith("subtitle"))
            if masked == 0: masks = None
        elif reg["kind"].startswith("manual"):
            masks = h29._manual_masks(frames, n, reg)
            masks = h29._limit_masks_range(masks, n, reg)
        else:
            masks = [reg["static_mask"]] * n
        crops[ri] = None
        del frames
        if masks is None: continue
        tmp_upload(pid, f"m{len(plan_regions)}.bin", h29.masks_pack(masks))
        fr = (reg["f0"], reg["f1"]) if reg["kind"].startswith("manual") and "f0" in reg else None
        chunks = h29.plan_text_chunks(masks, N, frame_range=fr)
        for c in chunks: all_chunks.append({"r": len(plan_regions), "s": c["s"], "e": c["e"]})
        reg2 = {k: v for k, v in reg.items() if k != "static_mask"}
        plan_regions.append(reg2)
        del masks
    sw.mark("masks")
    if not plan_regions or not all_chunks:
        h29.set_proj(pid, "wm_done", {"note": "no_target", "ver": V31_VER,
            "msg": "지울 자막을 찾지 못했어요. [직접 지정] 모드를 써주세요."})
        return {"note": "no_target"}
    # v31 세그먼트 계획 — 1차: 균등 프레임 분할 (v29 mergeseg와 동일 규칙: [k*N//K, (k+1)*N//K))
    K = max(1, min(int(seg_k), 16))
    segments = [[k * N // K, (k + 1) * N // K] for k in range(K)]
    plan = {"W": W, "H": H, "fps": info["fps"], "N": N, "audio": info["audio"],
            "mode": mode, "tier": proj.get("wm_tier") or "std", "cfr": cfr,
            "ver": V31_VER, "regions": plan_regions, "chunks": all_chunks,
            "segK": K, "segments": segments}
    tmp_upload(pid, "plan.json", json.dumps(plan).encode(), "application/json")
    sw.mark("plan_up")
    return {"phase": "plan_v31", "chunks": len(all_chunks), "regions": len(plan_regions),
            "N": N, "segments": segments, "ver": V31_VER, "tms": sw.out()}


# ---------------- 단계: segment (GPU) — AI 복원 + 즉시 합성 + 단일 인코딩 ----------------
def segment_v31(proj, tmp, part):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(pid, "plan.json").decode())
    W, H, fps, N = plan["W"], plan["H"], plan["fps"], plan["N"]
    K = plan["segK"]
    F0, F1 = plan["segments"][part]
    tier = h29.TIERS.get(plan["tier"], h29.TIERS["std"])
    src, work = fetch_lite_v31(proj, tmp, plan)
    sw.mark("dl")
    h29.get_pipe()
    sw.mark("model")

    # 담당 구간과 겹치는 소유권(ownership) → 필요한 조각과 crop 해독 범위 산출
    seg_rest = {}   # ri -> {frame_i: (h,w,3)}
    masks_all = {}  # ri -> 전체 masks (v1: 전체 다운로드; slice 최적화는 4단계)
    t_dec = 0.0; t_ai = 0.0
    counters = {"intermediate_ai_mp4_count": 0,
                "range_full_decode_count_per_segment": 0,
                "range_union_crop_decode_count_per_segment": 0}
    for ri, reg in enumerate(plan["regions"]):
        rcs = [c for c in plan["chunks"] if c["r"] == ri]
        if not rcs: continue
        own = [o for o in h29.ownership(rcs) if o["e"] >= F0 and o["s"] < F1]
        if not own: continue
        masks_all[ri] = h29.masks_unpack(tmp_download(pid, f"m{ri}.bin"))
        seg_rest[ri] = {}
        need = []   # 이 세그먼트가 계산할 조각 (중복 제거, s 오름차순)
        seen = set()
        for o in own:
            key = (o["c"]["s"], o["c"]["e"])
            if key not in seen:
                seen.add(key); need.append(o["c"])
        need.sort(key=lambda c: c["s"])
        crop_f0 = min(c["s"] for c in need)
        crop_f1 = max(c["e"] for c in need) + 1
        td = time.time()
        frames_local = read_crop_range(work, reg["x"], reg["y"], reg["w"], reg["h"],
                                       crop_f0, crop_f1, fps)
        t_dec += time.time() - td
        counters["range_union_crop_decode_count_per_segment"] += 1
        masks_local = masks_all[ri][crop_f0:crop_f1]
        t2 = dict(tier)
        if reg["kind"].startswith("manual") and plan.get("tier") != "fast":
            t2["scale"] = 1.0   # v29 규칙 유지
        # 조각별 AI 복원 (global→local 인덱스 변환 후 v29 restore_chunk 그대로 사용)
        arr_by_chunk = {}
        for c in need:
            lc = {"s": c["s"] - crop_f0, "e": c["e"] - crop_f0}
            assert 0 <= lc["s"] <= lc["e"] < len(frames_local), f"조각 범위 오류 {c} vs crop[{crop_f0},{crop_f1})"
            ta = time.time()
            arr_by_chunk[(c["s"], c["e"])] = h29.restore_chunk(frames_local, masks_local, t2, lc)
            t_ai += time.time() - ta
        del frames_local, masks_local
        # 소유 구간에 해당하는 프레임만 결과 보관 (v29 mergeseg와 동일한 귀속 규칙)
        rest = seg_rest[ri]
        for o in own:
            c = o["c"]; arr = arr_by_chunk[(c["s"], c["e"])]
            a = max(o["s"], F0); b = min(o["e"], F1 - 1)
            for i in range(a, b + 1):
                rest[i] = arr[i - c["s"]]
        del arr_by_chunk
    sw.t["crop_dec"] = round(t_dec, 1); sw.t["ai"] = round(t_ai, 1)
    sw.last = time.time()

    # 담당 구간 전체 프레임을 정확 해독하며 즉시 합성 → 단일 인코딩 (v29 mergeseg 합성식 그대로)
    outp = os.path.join(tmp, f"seg_{part}.mp4")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                            "-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                            outp, "-y"], stdin=subprocess.PIPE)
    counters["range_full_decode_count_per_segment"] += 1
    i = F0
    for fr in stream_frames_range(work, W, H, F0, F1, fps):
        frame = fr.copy()
        for ri, rest in seg_rest.items():
            if i not in rest: continue
            reg = plan["regions"][ri]
            if "f0" in reg and not (int(reg["f0"]) <= i < int(reg["f1"])): continue
            a = cv2.GaussianBlur(masks_all[ri][i], (0, 0), 6 if reg["kind"].startswith("manual") else 2).astype(np.float32)[..., None] / 255.0
            sub = frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]].astype(np.float32)
            frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = \
                np.clip(sub * (1 - a) + rest[i].astype(np.float32) * a, 0, 255).astype(np.uint8)
        enc.stdin.write(frame.tobytes())
        i += 1
    enc.stdin.close(); enc.wait()
    if enc.returncode != 0: raise RuntimeError("[v31] 세그먼트 합성 인코딩 실패")
    if i != F1: raise RuntimeError(f"[v31] 합성 프레임 수 불일치: {i - F0} != {F1 - F0}")
    sw.mark("comp_enc")
    with open(outp, "rb") as f:
        tmp_upload(pid, f"seg_{part}.mp4", f.read(), "video/mp4")
    os.remove(outp)
    sw.mark("up")
    try:
        h29.set_proj(pid, "wm_running", f"[v31] 구간 {part + 1}/{K} 복원·합성 완료")
    except Exception:
        pass
    return {"phase": "segment_v31", "part": part, "frames": F1 - F0,
            "counters": counters, "tms": sw.out()}


# ---------------- 단계: finish (CPU) ----------------
def finish_v31(proj, tmp, t0, parts, tms_in=None):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(pid, "plan.json").decode())
    N, fps = plan["N"], plan["fps"]
    h29.set_proj(pid, "wm_running", "[v31] 마무리 중…")
    from concurrent.futures import ThreadPoolExecutor
    def _dl_seg(k):
        p = os.path.join(tmp, f"seg_{k}.mp4")
        with open(p, "wb") as f: f.write(tmp_download(pid, f"seg_{k}.mp4"))
        return p
    with ThreadPoolExecutor(max_workers=6) as ex:
        seg_paths = list(ex.map(_dl_seg, range(parts)))
    sw.mark("dl")
    # 세그먼트 프레임 수 검증 (누락·중복 0 보장)
    total_seg_frames = 0
    for k, p in enumerate(seg_paths):
        c = h29.frame_count(p)
        exp = plan["segments"][k][1] - plan["segments"][k][0]
        if c != exp:
            raise RuntimeError(f"[v31] seg_{k} 프레임 수 {c} != 기대 {exp}")
        total_seg_frames += c
    if total_seg_frames != N:
        raise RuntimeError(f"[v31] 세그먼트 합계 {total_seg_frames} != N {N}")
    sw.mark("verify")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in seg_paths: f.write(f"file '{p}'\n")
    outp = os.path.join(tmp, "out.mp4")
    if plan.get("audio"):
        aurl = h29.signed_url(proj["source_path"], 21600)
        h29.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", aurl,
                 "-map", "0:v", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                 "-movflags", "+faststart", outp, "-y"])
    else:
        h29.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                 "-c", "copy", "-movflags", "+faststart", outp, "-y"])
    sw.mark("concat")
    fin_n = h29.frame_count(outp)
    if fin_n != N:
        raise RuntimeError(f"[v31] 최종 프레임 수 {fin_n} != N {N}")
    dest = f"{proj['user_id']}/wm_v31_{pid}.mp4"
    url_out = h29.upload_clip(dest, outp)
    sw.mark("up")
    sec = round(time.time() - t0)
    tms = dict(tms_in or {})
    tms["finish"] = sw.out()
    detail = {"url": url_out, "mode": plan.get("mode"), "tier": plan.get("tier"),
              "regions": [r["kind"] for r in plan["regions"]], "sec": sec,
              "gpu": BACKEND_NAME, "ver": V31_VER, "segK": plan.get("segK"), "tms": tms}
    h29.set_proj(pid, "wm_done", detail)
    names = ["plan.json"] + [f"m{ri}.bin" for ri in range(len(plan["regions"]))] \
        + [f"seg_{k}.mp4" for k in range(parts)] + (["work.mp4"] if plan.get("cfr") else [])
    tmp_delete(pid, names)
    print("[v31] 완료", pid, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
    return {"phase": "finish_v31", "ok": True, "sec": sec, "frames": fin_n, "tms": sw.out()}


# ---------------- warm (GPU 예열) ----------------
def warm_v31():
    t0 = time.time()
    if not h29._gpu_healthy():
        return {"warm": False, "error": "SICK_WORKER"}
    h29.get_pipe()
    import torch
    return {"warm": True, "model_load_s": round(time.time() - t0, 1),
            "gpu": torch.cuda.get_device_name(0), "container": os.environ.get("HOSTNAME", "?")}


# ---------------- 진입점 ----------------
def handler_v31(event):
    inp = (event or {}).get("input") or {}
    phase = inp.get("phase")
    t0 = float(inp.get("t0") or time.time())
    if phase == "warm_v31":
        return warm_v31()
    pid = inp.get("project_id")
    proj = h29.sb_select_one("sc_projects", {"id": "eq." + pid})
    if not proj:
        return {"error": f"프로젝트 없음: {pid}"}
    tmp = tempfile.mkdtemp(prefix="wmv31-")
    hb = None
    try:
        part = int(inp.get("part", 0))
        hb = h29._hb_start(pid, str(phase), part)
        if phase == "plan_v31":
            return plan_v31(proj, tmp, scan_step=int(inp.get("scan_step") or 12),
                            seg_k=int(inp.get("seg_k") or 5))
        if phase == "segment_v31":
            return segment_v31(proj, tmp, part)
        if phase == "finish_v31":
            return finish_v31(proj, tmp, t0, int(inp.get("parts") or 0), inp.get("tms"))
        return {"error": f"알 수 없는 phase: {phase}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"[v31:{phase}] {type(e).__name__}: {e}"}
    finally:
        if hb: hb.set()
        shutil.rmtree(tmp, ignore_errors=True)
