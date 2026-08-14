# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 자막·워터마크 제거기 — RunPod GPU 일꾼 v1.0
# 역할: project_id 하나를 받아 전체 처리(감지→AI복원→합성→업로드)를
#       GPU 서버 한 대 안에서 수행. Supabase 상태 갱신 포함.
# 입력: {"input": {"project_id": "..."}}
# ============================================================
import os, io, json, time, math, subprocess, tempfile, shutil, traceback
import numpy as np
import requests

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE", "")
MODEL_DIR = os.environ.get("MODEL_DIR", "/models")

TIERS = {
    "fast": {"scale": 0.5,  "steps": 4},
    "std":  {"scale": 0.75, "steps": 6},
    "hq":   {"scale": 1.0,  "steps": 8},
}
CHUNK_LEN = 201   # 4k+1 (로컬 GPU라 조각을 작게, 순차 처리)
CHUNK_STEP = 189  # 12프레임 겹침

# ---------------- Supabase REST ----------------
def sb_headers(extra=None):
    h = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY}
    if extra: h.update(extra)
    return h

def sb_update(table, match, data):
    r = requests.patch(f"{SB_URL}/rest/v1/{table}", params=match,
                       headers=sb_headers({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                       data=json.dumps(data), timeout=30)
    r.raise_for_status()

def sb_select_one(table, match, cols="*"):
    p = dict(match); p["select"] = cols
    r = requests.get(f"{SB_URL}/rest/v1/{table}", params=p, headers=sb_headers(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None

def set_proj(pid, status, detail):
    sb_update("sc_projects", {"id": "eq." + pid},
              {"status": status, "status_detail": detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False),
               "updated_at": now_iso()})

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def signed_url(path, expires):
    r = requests.post(f"{SB_URL}/storage/v1/object/sign/videos-source/{path}",
                      headers=sb_headers({"Content-Type": "application/json"}),
                      data=json.dumps({"expiresIn": expires}), timeout=30)
    r.raise_for_status()
    return SB_URL + "/storage/v1" + r.json()["signedURL"]

def upload_clip(path_in_bucket, filepath):
    with open(filepath, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/videos-clips/{path_in_bucket}",
                          headers=sb_headers({"Content-Type": "video/mp4", "x-upsert": "true"}),
                          data=f, timeout=600)
    r.raise_for_status()
    r2 = requests.post(f"{SB_URL}/storage/v1/object/sign/videos-clips/{path_in_bucket}",
                       headers=sb_headers({"Content-Type": "application/json"}),
                       data=json.dumps({"expiresIn": 86400}), timeout=30)
    r2.raise_for_status()
    return SB_URL + "/storage/v1" + r2.json()["signedURL"]

# ---------------- ffmpeg helpers ----------------
def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, **kw)

def probe_info(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]).stdout
    j = json.loads(out)
    v = next(s for s in j["streams"] if s.get("codec_type") == "video")
    num, den = (v.get("r_frame_rate") or "30/1").split("/")
    fps = round(float(num) / float(den or 1)) or 30
    if fps <= 0 or fps > 120: fps = 30
    dur = float(j.get("format", {}).get("duration") or 0)
    has_audio = any(s.get("codec_type") == "audio" for s in j["streams"])
    return {"W": int(v["width"]), "H": int(v["height"]), "fps": fps, "dur": dur, "audio": has_audio}

def frame_count(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
               "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path]).stdout
    return int(out.decode().strip() or 0)

def read_region_frames(path, x, y, w, h, sample_every=1, scale_to=None):
    """crop 영역의 프레임을 numpy (N,h,w,3) uint8 로 읽기 (rawvideo 파이프)"""
    vf = f"crop={w}:{h}:{x}:{y}"
    if sample_every > 1: vf = f"select='not(mod(n\\,{sample_every}))',{vf}"
    ow, oh = w, h
    if scale_to:
        ow, oh = scale_to
        vf += f",scale={ow}:{oh}"
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", vf,
                          "-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE)
    frames = []
    fsz = ow * oh * 3
    while True:
        buf = p.stdout.read(fsz)
        if not buf or len(buf) < fsz: break
        frames.append(np.frombuffer(buf, np.uint8).reshape(oh, ow, 3))
    p.wait()
    return frames

def snap16(n): return max(16, round(n / 16) * 16)
def floor16(n): return max(16, (int(n) // 16) * 16)
def clamp(v, a, b): return max(a, min(b, v))

# ---------------- 글자 감지 (wmremove.js 포팅, numpy 벡터화) ----------------
import cv2

def glyph_clusters(frame):
    """흰 글자(저채도 고명도)+검은 테두리 → CC → 가로줄 클러스터. frame: (h,w,3) uint8"""
    h, w = frame.shape[:2]
    r = frame[:, :, 0].astype(np.int32); g = frame[:, :, 1].astype(np.int32); b = frame[:, :, 2].astype(np.int32)
    mx = np.maximum(np.maximum(r, g), b); mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) * 255 // np.maximum(mx, 1), 0)
    white = ((mx > 200) & (sat < 50)).astype(np.uint8)
    dark = (mx < 75).astype(np.uint8)
    darkD = cv2.dilate(dark, np.ones((3, 3), np.uint8), iterations=4)
    nlab, lab, stats, _ = cv2.connectedComponentsWithStats(white, 8)
    good = []
    for i in range(1, nlab):
        x, y, cw, ch, area = stats[i]
        if not (20 <= area <= 7000 and 8 <= ch <= 85 and cw <= 240): continue
        m = (lab == i)
        sig = int(np.count_nonzero(m & (darkD > 0)))
        if sig < max(4, 0.06 * area): continue
        good.append({"x0": x, "x1": x + cw - 1, "y0": y, "y1": y + ch - 1, "h": ch, "mask": m})
    good.sort(key=lambda c: c["y0"] + c["y1"])
    clusters = []
    for c in good:
        cy = (c["y0"] + c["y1"]) / 2
        put = None
        for cl in clusters:
            if abs(cl["cy"] - cy) < 25: put = cl; break
        if put:
            put["items"].append(c)
            put["cy"] = sum((i["y0"] + i["y1"]) / 2 for i in put["items"]) / len(put["items"])
        else:
            clusters.append({"cy": cy, "items": [c]})
    out = []
    for cl in clusters:
        its = cl["items"]
        if len(its) < 2: continue
        x0 = min(i["x0"] for i in its); x1 = max(i["x1"] for i in its)
        if x1 - x0 < 90: continue
        hs = sorted(i["h"] for i in its); med = hs[len(hs) // 2]
        if med < 16 or med > 80: continue
        if x0 < 0.07 * w or x1 > 0.93 * w: continue
        y0 = min(i["y0"] for i in its); y1 = max(i["y1"] for i in its)
        out.append({"x0": x0, "x1": x1, "y0": y0, "y1": y1, "items": its, "dark": dark})
    return out

def iou(a, b):
    x1 = max(a["x0"], b["x0"]); y1 = max(a["y0"], b["y0"])
    x2 = min(a["x1"], b["x1"]); y2 = min(a["y1"], b["y1"])
    if x2 <= x1 or y2 <= y1: return 0.0
    inter = (x2 - x1) * (y2 - y1)
    aa = (a["x1"] - a["x0"]) * (a["y1"] - a["y0"]); bb = (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
    return inter / (aa + bb - inter)

def rasterize(clusters, w, h):
    """글자픽셀 + (글자 8px 이내 어두운 테두리) → 4px 팽창 마스크 (h,w) uint8 0/255"""
    m = np.zeros((h, w), np.uint8)
    dark = clusters[0]["dark"] if clusters else None
    glyph = np.zeros((h, w), np.uint8)
    for cl in clusters:
        for it in cl["items"]:
            glyph |= it["mask"].astype(np.uint8)
    gd = cv2.dilate(glyph, np.ones((3, 3), np.uint8), iterations=8)
    m = (glyph | (gd & (dark.astype(np.uint8) if dark is not None else 0))).astype(np.uint8)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=4)
    return (m * 255).astype(np.uint8)

def detect_sub_band(work, W, H, N):
    """하단 45%에서 자막 띠 찾기 (12프레임 간격 샘플)"""
    y0 = int(H * 0.55)
    frames = read_region_frames(work, 0, y0, W, H - y0, sample_every=12)
    hits = []
    for f in frames:
        for c in glyph_clusters(f):
            hits.append((c["y0"] + y0, c["y1"] + y0))
    if len(hits) < 4: return None
    ys = sorted((a + b) / 2 for a, b in hits)
    mid = ys[len(ys) // 2]
    top = max(0, int(min(a for a, b in hits if abs((a + b) / 2 - mid) < 120) - 40))
    bot = min(H, int(max(b for a, b in hits if abs((a + b) / 2 - mid) < 120) + 40))
    bh = floor16(bot - top)
    if bh < 48: bh = 48
    bw = floor16(W)
    bx = (W - bw) // 2
    by = clamp(top, 0, H - bh)
    return {"x": bx, "y": by, "w": bw, "h": bh, "kind": "subtitle"}

def detect_corner(work, W, H, side, N):
    """모서리 고정 무늬 감지 (정지장면 오탐 방지 게이트 포함)"""
    cw = floor16(int(W * 0.42)); ch = floor16(int(H * 0.28))
    cx = 0 if side == "tl" else W - cw
    frames = read_region_frames(work, cx, 0, cw, ch, sample_every=50)
    frames = frames[:30]
    if len(frames) < 6: return None
    gray = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in frames])  # (k,ch,cw)
    mx = gray.max(0).astype(np.int32); mn = gray.min(0).astype(np.int32)
    static = (mx - mn) <= 20
    if static.mean() > 0.55: return None  # 화면 자체가 정지 → 오탐 방지
    med = np.median(gray, 0).astype(np.int32)
    gx = np.abs(np.diff(med, axis=1)); gx = np.pad(gx, ((0, 0), (0, 1)))
    gy = np.abs(np.diff(med, axis=0)); gy = np.pad(gy, ((0, 1), (0, 0)))
    sig = (static & ((gx + gy) > 14))
    ratio = sig.mean()
    if ratio < 0.004 or ratio > 0.08: return None
    d = cv2.dilate(sig.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=6)
    tot = int(d.sum())
    n = cw * ch
    if tot < 400 or tot > n * 0.25: return None
    ys, xs = np.where(d > 0)
    if (xs.max() - xs.min()) > cw * 0.85 or (ys.max() - ys.min()) > ch * 0.85: return None
    return {"x": cx, "y": 0, "w": cw, "h": ch, "kind": "corner-left" if side == "tl" else "corner-right",
            "static_mask": (d * 255).astype(np.uint8)}

def build_subtitle_masks(band_frames, N):
    """프레임별 클러스터 → 시간 안정성(±6 중 5) → ±6 링 합집합 마스크 목록"""
    per = [glyph_clusters(f) for f in band_frames]
    def stable(i, box):
        cnt = 0
        for j in range(max(0, i - 6), min(N - 1, i + 6) + 1):
            if j == i: continue
            if any(iou(box, b) > 0.3 for b in per[j]): cnt += 1
        return cnt >= 5
    h, w = band_frames[0].shape[:2]
    raw = []
    masked = 0
    for i in range(N):
        keep = [c for c in per[i] if stable(i, c)]
        if keep:
            raw.append(rasterize(keep, w, h)); masked += 1
        else:
            raw.append(np.zeros((h, w), np.uint8))
    out = []
    for i in range(N):
        u = np.zeros((h, w), np.uint8)
        for j in range(max(0, i - 6), min(N - 1, i + 6) + 1):
            u |= raw[j]
        out.append(u)
    return out, masked

# ---------------- AI 파이프라인 (로컬 GPU) ----------------
_PIPE = None
def get_pipe():
    global _PIPE
    if _PIPE is None:
        import torch
        from diffusers.models import AutoencoderKLWan
        from diffusers.schedulers import UniPCMultistepScheduler
        from transformer_minimax_remover import Transformer3DModel
        from pipeline_minimax_remover import Minimax_Remover_Pipeline
        vae = AutoencoderKLWan.from_pretrained(os.path.join(MODEL_DIR, "vae"), torch_dtype=torch.float16)
        transformer = Transformer3DModel.from_pretrained(os.path.join(MODEL_DIR, "transformer"), torch_dtype=torch.float16)
        scheduler = UniPCMultistepScheduler.from_pretrained(os.path.join(MODEL_DIR, "scheduler"))
        _PIPE = Minimax_Remover_Pipeline(transformer=transformer, vae=vae, scheduler=scheduler)
        _PIPE.to("cuda:0")
    return _PIPE

def restore_region(frames, masks, tier, on_step=None):
    """frames:(N,h,w,3)uint8 masks:(N,h,w)0/255 → 복원된 (N,h,w,3)uint8"""
    import torch
    pipe = get_pipe()
    N = len(frames)
    h, w = frames[0].shape[:2]
    sw = snap16(w * tier["scale"]); sh = snap16(h * tier["scale"])
    starts = []
    if N <= CHUNK_LEN: starts = [0]
    else:
        s = 0
        while s + CHUNK_LEN <= N: starts.append(s); s += CHUNK_STEP
        if starts[-1] + CHUNK_LEN < N: starts.append(N - CHUNK_LEN)
    chunks = [{"i": i, "s": s, "e": min(N - 1, s + CHUNK_LEN - 1)} for i, s in enumerate(starts)]
    outs = []
    for c in chunks:
        n = c["e"] - c["s"] + 1
        n_use = ((n - 1) // 4) * 4 + 1  # 4k+1
        imgs = np.stack(frames[c["s"]:c["s"] + n_use]).astype(np.float32) / 127.5 - 1.0
        mks = (np.stack(masks[c["s"]:c["s"] + n_use]) > 20).astype(np.float32)[..., None]
        t_img = torch.from_numpy(imgs)
        t_mk = torch.from_numpy(mks)
        result = pipe(images=t_img, masks=t_mk, num_frames=n_use, height=sh, width=sw,
                      num_inference_steps=tier["steps"],
                      generator=torch.Generator(device="cuda:0").manual_seed(42),
                      iterations=6).frames[0]
        arr = (np.clip(result, 0, 1) * 255).astype(np.uint8)  # (n_use, sh, sw, 3)
        if (sh, sw) != (h, w):
            arr = np.stack([cv2.resize(fr, (w, h), interpolation=cv2.INTER_LANCZOS4) for fr in arr])
        if n_use < n:
            arr = np.concatenate([arr, np.repeat(arr[-1:], n - n_use, 0)])
        outs.append({"s": c["s"], "e": c["e"], "arr": arr})
        if on_step: on_step(len(outs), len(chunks))
    # 겹침(12프레임) 중앙에서 이어붙이기: k-1은 s_k+5까지, k는 s_k+6부터
    merged = np.zeros((N, h, w, 3), np.uint8)
    for k, o in enumerate(outs):
        s_use = 0 if k == 0 else o["s"] + 6
        e_use = o["e"] if k == len(outs) - 1 else outs[k + 1]["s"] + 5
        merged[s_use:e_use + 1] = o["arr"][s_use - o["s"]:e_use - o["s"] + 1]
    return merged

# ---------------- 메인 처리 ----------------
def process(project_id):
    t0 = time.time()
    proj = sb_select_one("sc_projects", {"id": "eq." + project_id})
    if not proj: raise RuntimeError("프로젝트를 찾을 수 없어요: " + project_id)
    tier = TIERS.get(proj.get("wm_tier") or "std", TIERS["std"])
    mode = "manual" if proj.get("wm_mode") == "manual" else "auto"
    tmp = tempfile.mkdtemp(prefix="ibwm-")
    try:
        set_proj(project_id, "wm_running", "영상을 받아 오는 중…")
        src = os.path.join(tmp, "src.mp4")
        url = signed_url(proj["source_path"], 21600)
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(src, "wb") as f:
                for chunk in r.iter_content(1024 * 1024): f.write(chunk)
        info = probe_info(src)
        if info["dur"] > 900: raise RuntimeError("지금은 15분 이하 영상만 지원해요. 나눠서 올려주세요.")
        set_proj(project_id, "wm_running", "영상을 분석하는 중…")
        # CFR 보정: 대부분 CFR이라 그대로 사용, 아니면 초고속 재인코딩
        work = src
        N = frame_count(work)
        W, H, fps = info["W"], info["H"], info["fps"]
        expected = round(info["dur"] * fps)
        if N and abs(N - expected) > max(5, expected * 0.02):
            work = os.path.join(tmp, "work.mp4")
            run(["ffmpeg", "-v", "error", "-i", src, "-vf", f"fps={fps}", "-an",
                 "-c:v", "libx264", "-crf", "12", "-preset", "ultrafast", "-pix_fmt", "yuv420p", work, "-y"])
            N = frame_count(work)
        # ---- 영역 결정 ----
        regions = []
        if mode == "manual":
            rects = proj.get("wm_rects") or []
            if not rects: raise RuntimeError("지울 영역이 지정되지 않았어요")
            for r0 in rects[:4]:
                px = clamp(round(r0["x"] * W), 0, W - 8); py = clamp(round(r0["y"] * H), 0, H - 8)
                pw = clamp(round(r0["w"] * W), 8, W - px); ph = clamp(round(r0["h"] * H), 8, H - py)
                gx = clamp(px - 32, 0, W); gy = clamp(py - 32, 0, H)
                gw = floor16(min(W - gx, pw + 64)); gh = floor16(min(H - gy, ph + 64))
                if gx + gw > W: gx = W - gw
                if gy + gh > H: gy = H - gh
                regions.append({"x": gx, "y": gy, "w": gw, "h": gh, "kind": f"manual{len(regions)}",
                                "rx0": px - gx, "rx1": px - gx + pw - 1, "ry0": py - gy, "ry1": py - gy + ph - 1})
        else:
            set_proj(project_id, "wm_running", "자막·워터마크를 찾는 중…")
            band = detect_sub_band(work, W, H, N)
            if band: regions.append(band)
            for side in ("tl", "tr"):
                c = detect_corner(work, W, H, side, N)
                if c: regions.append(c)
            if not regions:
                set_proj(project_id, "wm_done", {"note": "no_target",
                    "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
                return {"note": "no_target"}
        # ---- 마스크 + 복원 ----
        results = []
        done_regions = 0
        for reg in regions:
            frames = read_region_frames(work, reg["x"], reg["y"], reg["w"], reg["h"])
            n = len(frames)
            if reg["kind"] == "subtitle":
                set_proj(project_id, "wm_running", "자막 글자 위치를 정밀하게 잡는 중…")
                masks, masked = build_subtitle_masks(frames, n)
                if masked == 0: continue
            elif reg["kind"].startswith("manual"):
                set_proj(project_id, "wm_running", "지정한 곳의 글자를 정밀하게 찾는 중…")
                masks, masked = build_subtitle_masks(frames, n)
                if masked < max(10, math.ceil(n * 0.03)):
                    m = np.zeros((reg["h"], reg["w"]), np.uint8)
                    pad = 12
                    m[max(0, reg["ry0"] - pad):min(reg["h"], reg["ry1"] + pad + 1),
                      max(0, reg["rx0"] - pad):min(reg["w"], reg["rx1"] + pad + 1)] = 255
                    masks = [m] * n
            else:
                masks = [reg["static_mask"]] * n
            set_proj(project_id, "wm_running", "AI가 배경을 복원하는 중…")
            def on_step(d, t):
                try: set_proj(project_id, "wm_running", f"AI가 배경을 복원하는 중… ({d}/{t} 조각)")
                except Exception: pass
            merged = restore_region(frames, masks, tier, on_step)
            results.append({"reg": reg, "restored": merged, "masks": masks})
            done_regions += 1
        if not results:
            set_proj(project_id, "wm_done", {"note": "no_target",
                "msg": "지울 자막을 찾지 못했어요. [직접 지정] 모드를 써주세요."})
            return {"note": "no_target"}
        # ---- 합성 (스트리밍) ----
        set_proj(project_id, "wm_running", "복원한 부분을 원본에 합치는 중…")
        outp = os.path.join(tmp, "out.mp4")
        dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", work, "-vsync", "0",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
        enc_cmd = ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{W}x{H}", "-r", str(fps), "-i", "-"]
        if info["audio"]:
            enc_cmd += ["-i", src, "-map", "0:v", "-map", "1:a:0", "-c:a", "aac", "-b:a", "160k"]
        enc_cmd += ["-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", outp, "-y"]
        enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)
        fsz = W * H * 3
        i = 0
        while True:
            buf = dec.stdout.read(fsz)
            if not buf or len(buf) < fsz: break
            frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
            for res in results:
                reg = res["reg"]
                if i >= len(res["restored"]): continue
                a = cv2.GaussianBlur(res["masks"][i], (0, 0), 2).astype(np.float32)[..., None] / 255.0
                sub = frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]].astype(np.float32)
                rest = res["restored"][i].astype(np.float32)
                frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = \
                    np.clip(sub * (1 - a) + rest * a, 0, 255).astype(np.uint8)
            enc.stdin.write(frame.tobytes())
            i += 1
        dec.wait(); enc.stdin.close(); enc.wait()
        if enc.returncode != 0: raise RuntimeError("최종 합성 인코딩 실패")
        # ---- 업로드 + 마무리 ----
        dest = f"{proj['user_id']}/wm_{project_id}.mp4"
        url_out = upload_clip(dest, outp)
        # 사용량 +1
        ym = time.strftime("%Y-%m", time.gmtime())
        try:
            r = requests.post(f"{SB_URL}/rest/v1/wm_usage",
                              params={"on_conflict": "user_id,ym"},
                              headers=sb_headers({"Content-Type": "application/json",
                                                  "Prefer": "resolution=ignore-duplicates,return=minimal"}),
                              data=json.dumps({"user_id": proj["user_id"], "ym": ym, "used": 0}), timeout=30)
            cur = sb_select_one("wm_usage", {"user_id": "eq." + proj["user_id"], "ym": "eq." + ym}, "id,used")
            if cur: sb_update("wm_usage", {"id": "eq." + str(cur["id"])}, {"used": (cur.get("used") or 0) + 1})
        except Exception:
            traceback.print_exc()
        sec = round(time.time() - t0)
        detail = {"url": url_out, "mode": mode, "tier": proj.get("wm_tier") or "std",
                  "regions": [r["reg"]["kind"] for r in results], "sec": sec, "gpu": "runpod"}
        set_proj(project_id, "wm_done", detail)
        print("[gpu-wm] 완료", project_id, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
        return detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ---------------- RunPod 진입점 ----------------
def handler(event):
    pid = (event.get("input") or {}).get("project_id")
    if not pid: return {"error": "project_id가 없습니다"}
    try:
        return process(pid)
    except Exception as e:
        traceback.print_exc()
        try:
            set_proj(pid, "failed_wm", str(e)[:300])
        except Exception:
            pass
        return {"error": str(e)[:500]}

if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": handler})
