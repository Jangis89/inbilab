# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 자막·워터마크 제거기 — V32 경쟁속도 파이프라인 (스테이징)
# 설계 (V32 명세):
#   1) 중앙 plan에서 전 프레임 정밀 마스크 생성 폐지 → scan은 영역·기본정보만 (수십 초)
#   2) 마스크 생성은 각 GPU segment worker가 자기 구간만 수행 (분산·스트리밍)
#   3) 정밀 감지는 key frame(기본 5프레임 간격)만, 사이 프레임은 시간축 union으로 전파
#      (v29도 ±6 프레임 union을 쓰므로 시간축 전파는 기존 동작의 일반화)
#   4) V31의 exact range decode / 통합 segment worker / 단일 인코딩 / finish 구조 재사용
#   5) 품질 판정은 byte 동일이 아니라 실측 게이트 (frame/audio/PSNR/SSIM/경계) — 명세 18장
# 주의: bitwise 동일성은 목표가 아님(사장님 승인 2026-08-17). V31이 golden 기준선.
# ============================================================
import os, json, time, subprocess, tempfile, shutil, traceback, base64
import numpy as np

import handler as h29        # v29 함수 재사용
import handler_v31 as v31    # exact range decode + 공유메모리 감지 풀 재사용

V32_VER = "v32"
PFX = "wmtmp-v32"
BACKEND_NAME = os.environ.get("WM_BACKEND_NAME", "modal-v32")
KEY_STEP_DEF = 5             # 정밀 감지 키프레임 간격 (A/B: 1=완전 정밀, 3, 5, 8)

cv2 = h29.cv2
SW = h29.SW


# ---------------- v32 임시 저장 ----------------
def tmp_upload(pid, name, data, ctype="application/octet-stream"):
    h29.tmp_upload(f"{PFX}/{pid}/{name}", data, ctype)

def tmp_download(pid, name):
    return h29.tmp_download(f"{PFX}/{pid}/{name}")

def tmp_delete(pid, names):
    h29.tmp_delete(f"{PFX}/{pid}", names)


def fetch_lite_v32(proj, tmp, plan):
    pid = proj["id"]
    src = h29.cache_get(pid + "-src.mp4")
    if not src:
        src = os.path.join(tmp, "src.mp4")
        h29.download_to(h29.signed_url(proj["source_path"], 21600), src)
        h29.cache_put(pid + "-src.mp4", src)
    work = src
    if plan.get("cfr"):
        work = h29.cache_get(pid + "-work32.mp4")
        if not work:
            work = os.path.join(tmp, "work.mp4")
            with open(work, "wb") as f:
                f.write(tmp_download(pid, "work.mp4"))
            h29.cache_put(pid + "-work32.mp4", work)
    return src, work


def _pack_static(mask):
    bits = np.packbits(mask > 20)
    return {"static_pack": base64.b64encode(bits.tobytes()).decode(),
            "static_shape": list(mask.shape)}

def _unpack_static(reg):
    hh, ww = reg["static_shape"]
    bits = np.frombuffer(base64.b64decode(reg["static_pack"]), np.uint8)
    return (np.unpackbits(bits, count=hh * ww).reshape(hh, ww) * 255).astype(np.uint8)


# ---------------- 단계: scan (가벼운 계획 — 영역·구간만, 마스크 없음) ----------------
def scan_v32(proj, tmp, scan_step=12, seg_k=10):
    pid = proj["id"]
    sw = SW()
    h29.set_proj(pid, "wm_running", "[v32] 영상을 받아 오는 중…")
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
        h29.set_proj(pid, "wm_running", "[v32] 자막·워터마크 위치를 찾는 중…")
        samples = list(h29.stream_frames(work, W, H, sample_every=scan_step))
        sw.mark("scan_dec")
        regions.extend(h29.detect_sub_bands_from(samples, W, H))
        for side in ("tl", "tr"):
            c = h29.detect_corner_from(samples, W, H, side)
            if c: regions.append(c)
        del samples
        sw.mark("scan")
    if not regions:
        h29.set_proj(pid, "wm_done", {"note": "no_target", "ver": V32_VER,
            "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
        return {"note": "no_target"}
    plan_regions = []
    for reg in regions:
        reg2 = {k: v for k, v in reg.items() if k != "static_mask"}
        if reg.get("static_mask") is not None:
            reg2.update(_pack_static(reg["static_mask"]))
        plan_regions.append(reg2)
    K = max(1, min(int(seg_k), 16))
    segments = [[k * N // K, (k + 1) * N // K] for k in range(K)]
    plan = {"W": W, "H": H, "fps": info["fps"], "N": N, "audio": info["audio"],
            "mode": mode, "tier": proj.get("wm_tier") or "std", "cfr": cfr,
            "ver": V32_VER, "regions": plan_regions, "segK": K, "segments": segments}
    tmp_upload(pid, "plan.json", json.dumps(plan).encode(), "application/json")
    sw.mark("plan_up")
    return {"phase": "scan_v32", "regions": len(plan_regions), "N": N,
            "segments": segments, "ver": V32_VER, "tms": sw.out()}


# ---------------- 세그먼트-로컬 마스크 (키프레임 정밀 + 시간축 전파) ----------------
def _seg_masks_for_region(frames_local, reg, key_step, e0_global):
    """구간 crop 프레임에서 v29와 같은 glyph 감지를 키프레임만 수행하고
    안정성 검사·±ring union으로 전 프레임 마스크를 만든다.
    반환: (masks 목록(u8), 글자 있는 키프레임 수)"""
    n = len(frames_local)
    kind = reg["kind"]
    if kind.startswith("manual"):
        # 직접 지정: v29 로직 그대로 (전역 f0/f1을 로컬로 이동)
        reg_l = dict(reg)
        if "f0" in reg:
            reg_l["f0"] = max(0, int(reg["f0"]) - e0_global)
            reg_l["f1"] = max(0, int(reg["f1"]) - e0_global)
        if reg.get("ctx_boxes"):
            reg_l["ctx_boxes"] = [dict(b, f0=max(0, b["f0"] - e0_global), f1=max(0, b["f1"] - e0_global))
                                  for b in reg["ctx_boxes"]]
        masks = h29._manual_masks(frames_local, n, reg_l)
        masks = h29._limit_masks_range(masks, n, reg_l)
        return masks, sum(1 for m in masks if m is not None and m.any())
    if "static_pack" in reg:
        m = _unpack_static(reg)
        return [m] * n, n
    # 자막/라벨 밴드: 키프레임 정밀 감지 (공유메모리 풀 — v31이 h29._par_sweep에 이식)
    keys = list(range(0, n, max(1, int(key_step))))
    per = [None] * n
    res = h29._par_sweep("cl", frames_local, n, max(1, int(key_step)))
    if res is None:
        res = [h29.glyph_clusters(frames_local[i]) for i in keys]
    for k, i in enumerate(keys):
        per[i] = res[k]
    # 안정성: ±6 창 안에서 '계산된 이웃' 중 v29와 같은 비율(≈42%)이 일치해야 채택
    def stable(i, box):
        avail = 0; cnt = 0
        for j in range(max(0, i - 6), min(n - 1, i + 6) + 1):
            if j == i or per[j] is None: continue
            avail += 1
            if any(h29.iou(box, b) > 0.3 for b in per[j]): cnt += 1
        if avail == 0: return False
        req = max(1, int(0.42 * avail + 0.5))
        return cnt >= req
    hh, ww = frames_local[0].shape[:2]
    raw = [None] * n
    masked = 0
    for i in keys:
        keep = [c for c in per[i] if stable(i, c)]
        if keep:
            raw[i] = h29.rasterize(keep, ww, hh)
            masked += 1
    # ±(6 + key_step - 1) union — 키프레임 간격만큼 넓혀 v29 ±6 union의 커버리지 보존
    ring = 6 + max(1, int(key_step)) - 1
    zeros = np.zeros((hh, ww), np.uint8)
    out = []
    for i in range(n):
        u = None
        for j in range(max(0, i - ring), min(n - 1, i + ring) + 1):
            if raw[j] is not None:
                u = raw[j].copy() if u is None else (u | raw[j])
        out.append(u if u is not None else zeros)
    return out, masked


# ---------------- 단계: segment (GPU) — 로컬 마스크 + AI 복원 + 합성 + 인코딩 ----------------
def segment_v32(proj, tmp, part, key_step=KEY_STEP_DEF):
    pid = proj["id"]
    sw = SW()
    # GPU 상자(cpu=8)에서는 감지 풀을 코어 수에 맞춘다
    try:
        h29.NPROC = max(2, int(os.environ.get("WM_SEG_NPROC", "6")))
    except ValueError:
        pass
    plan = json.loads(tmp_download(pid, "plan.json").decode())
    W, H, fps, N = plan["W"], plan["H"], plan["fps"], plan["N"]
    K = plan["segK"]
    F0, F1 = plan["segments"][part]
    EXT = 6 + max(1, int(key_step)) + 12          # ring 문맥 + 조각 겹침 여유
    E0, E1 = max(0, F0 - EXT), min(N, F1 + EXT)
    tier = h29.TIERS.get(plan["tier"], h29.TIERS["std"])
    src, work = fetch_lite_v32(proj, tmp, plan)
    sw.mark("dl")
    h29.get_pipe()
    sw.mark("model")

    counters = {"intermediate_ai_mp4_count": 0, "key_step": int(key_step),
                "precise_keyframes": 0, "regions_active": 0}
    seg_rest = {}     # ri -> {global_i: 복원 crop}
    local_masks = {}  # ri -> 로컬 마스크 목록 (index = global_i - E0)
    t_dec = t_mask = t_ai = 0.0
    nl = E1 - E0
    for ri, reg in enumerate(plan["regions"]):
        td = time.time()
        frames_local = v31.read_crop_range(work, reg["x"], reg["y"], reg["w"], reg["h"],
                                           E0, E1, fps)
        t_dec += time.time() - td
        tm = time.time()
        masks, masked = _seg_masks_for_region(frames_local, reg, key_step, E0)
        t_mask += time.time() - tm
        counters["precise_keyframes"] += masked
        if masked == 0 or not any(m.any() for m in masks):
            del frames_local
            continue
        counters["regions_active"] += 1
        local_masks[ri] = masks
        fr_gate = None
        if reg["kind"].startswith("manual") and "f0" in reg:
            fr_gate = (max(0, int(reg["f0"]) - E0), max(0, int(reg["f1"]) - E0))
        chunks = h29.plan_text_chunks(masks, nl, frame_range=fr_gate)
        t2 = dict(tier)
        if reg["kind"].startswith("manual") and plan.get("tier") != "fast":
            t2["scale"] = 1.0
        rest = {}
        for c in chunks:
            # 이 세그먼트 소유 프레임과 무관한 조각은 건너뜀
            if c["e"] + E0 < F0 or c["s"] + E0 >= F1:
                continue
            ta = time.time()
            arr = h29.restore_chunk(frames_local, masks, t2, c)
            t_ai += time.time() - ta
            a = max(c["s"] + E0, F0); b = min(c["e"] + E0, F1 - 1)
            for gi in range(a, b + 1):
                rest[gi] = arr[gi - E0 - c["s"]]
            del arr
        if rest:
            seg_rest[ri] = rest
        del frames_local
    sw.t["crop_dec"] = round(t_dec, 1)
    sw.t["mask"] = round(t_mask, 1)
    sw.t["ai"] = round(t_ai, 1)
    sw.last = time.time()

    # 합성 + 단일 인코딩 (v31과 동일 구조, 마스크는 로컬 생성본)
    outp = os.path.join(tmp, f"seg_{part}.mp4")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                            "-c:v", "libx264", "-crf", "17", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", outp, "-y"], stdin=subprocess.PIPE)
    i = F0
    for fr in v31.stream_frames_range(work, W, H, F0, F1, fps):
        frame = fr.copy()
        for ri, rest in seg_rest.items():
            if i not in rest: continue
            reg = plan["regions"][ri]
            if "f0" in reg and not (int(reg["f0"]) <= i < int(reg["f1"])): continue
            m = local_masks[ri][i - E0]
            a = cv2.GaussianBlur(m, (0, 0), 6 if reg["kind"].startswith("manual") else 2)\
                .astype(np.float32)[..., None] / 255.0
            sub = frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]].astype(np.float32)
            frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = \
                np.clip(sub * (1 - a) + rest[i].astype(np.float32) * a, 0, 255).astype(np.uint8)
        enc.stdin.write(frame.tobytes())
        i += 1
    enc.stdin.close(); enc.wait()
    if enc.returncode != 0: raise RuntimeError("[v32] 세그먼트 합성 인코딩 실패")
    if i != F1: raise RuntimeError(f"[v32] 합성 프레임 수 불일치: {i - F0} != {F1 - F0}")
    sw.mark("comp_enc")
    with open(outp, "rb") as f:
        tmp_upload(pid, f"seg_{part}.mp4", f.read(), "video/mp4")
    os.remove(outp)
    sw.mark("up")
    try:
        h29.set_proj(pid, "wm_running", f"[v32] 구간 {part + 1}/{K} 복원·합성 완료")
    except Exception:
        pass
    return {"phase": "segment_v32", "part": part, "frames": F1 - F0,
            "counters": counters, "tms": sw.out()}


# ---------------- 단계: finish (CPU) ----------------
def finish_v32(proj, tmp, t0, parts, tms_in=None):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(pid, "plan.json").decode())
    N, fps = plan["N"], plan["fps"]
    h29.set_proj(pid, "wm_running", "[v32] 마무리 중…")
    from concurrent.futures import ThreadPoolExecutor
    def _dl_seg(k):
        p = os.path.join(tmp, f"seg_{k}.mp4")
        with open(p, "wb") as f: f.write(tmp_download(pid, f"seg_{k}.mp4"))
        return p
    with ThreadPoolExecutor(max_workers=6) as ex:
        seg_paths = list(ex.map(_dl_seg, range(parts)))
    sw.mark("dl")
    with ThreadPoolExecutor(max_workers=min(8, max(1, parts))) as ex:
        counts = list(ex.map(h29.frame_count, seg_paths))
    total_seg_frames = 0
    for k, c in enumerate(counts):
        exp = plan["segments"][k][1] - plan["segments"][k][0]
        if c != exp:
            raise RuntimeError(f"[v32] seg_{k} 프레임 수 {c} != 기대 {exp}")
        total_seg_frames += c
    if total_seg_frames != N:
        raise RuntimeError(f"[v32] 세그먼트 합계 {total_seg_frames} != N {N}")
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
        raise RuntimeError(f"[v32] 최종 프레임 수 {fin_n} != N {N}")
    dest = f"{proj['user_id']}/wm_v32_{pid}.mp4"
    url_out = h29.upload_clip(dest, outp)
    sw.mark("up")
    sec = round(time.time() - t0)
    tms = dict(tms_in or {})
    tms["finish"] = sw.out()
    detail = {"url": url_out, "mode": plan.get("mode"), "tier": plan.get("tier"),
              "regions": [r["kind"] for r in plan["regions"]], "sec": sec,
              "gpu": BACKEND_NAME, "ver": V32_VER, "segK": plan.get("segK"), "tms": tms}
    h29.set_proj(pid, "wm_done", detail)
    names = ["plan.json"] + [f"seg_{k}.mp4" for k in range(parts)] \
        + (["work.mp4"] if plan.get("cfr") else [])
    tmp_delete(pid, names)
    print("[v32] 완료", pid, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
    return {"phase": "finish_v32", "ok": True, "sec": sec, "frames": fin_n, "tms": sw.out()}


# ---------------- warm ----------------
def warm_v32():
    return v31.warm_v31()


# ---------------- 진입점 ----------------
def handler_v32(event):
    inp = (event or {}).get("input") or {}
    phase = inp.get("phase")
    t0 = float(inp.get("t0") or time.time())
    if phase == "warm_v32":
        return warm_v32()
    pid = inp.get("project_id")
    proj = h29.sb_select_one("sc_projects", {"id": "eq." + pid})
    if not proj:
        return {"error": f"프로젝트 없음: {pid}"}
    tmp = tempfile.mkdtemp(prefix="wmv32-")
    hb = None
    try:
        part = int(inp.get("part", 0))
        hb = h29._hb_start(pid, str(phase), part)
        if phase == "scan_v32":
            return scan_v32(proj, tmp, scan_step=int(inp.get("scan_step") or 12),
                            seg_k=int(inp.get("seg_k") or 10))
        if phase == "segment_v32":
            return segment_v32(proj, tmp, part,
                               key_step=int(inp.get("key_step") or KEY_STEP_DEF))
        if phase == "finish_v32":
            return finish_v32(proj, tmp, t0, int(inp.get("parts") or 0), inp.get("tms"))
        return {"error": f"알 수 없는 phase: {phase}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"[v32:{phase}] {type(e).__name__}: {e}"}
    finally:
        if hb: hb.set()
        shutil.rmtree(tmp, ignore_errors=True)
