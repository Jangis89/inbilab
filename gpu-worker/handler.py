# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 자막·워터마크 제거기 — RunPod GPU 일꾼 v2.0
# v2: 스톱워치(단계별 초) + 멀티코어 감지 + 단일 해독 재사용 +
#     합치기 병렬(mergeseg×N → finish 이어붙이기)
# 입력: {"input": {"project_id": "...", "phase": "plan|work|mergeseg|finish|merge|all"}}
# ============================================================
import os, io, json, time, math, subprocess, tempfile, shutil, traceback
import numpy as np
import requests
from multiprocessing import Pool

NPROC = max(2, min(12, (os.cpu_count() or 8) - 2))

class SW:
    """스톱워치: mark('이름')를 부를 때마다 직전 구간의 시간을 기록"""
    def __init__(self):
        self.t = {}
        self.last = time.time()
    def mark(self, name):
        now = time.time()
        self.t[name] = round(self.t.get(name, 0) + (now - self.last), 1)
        self.last = now
    def out(self):
        return self.t

SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE", "")
MODEL_DIR = os.environ.get("MODEL_DIR", "/models")

TIERS = {
    "fast": {"scale": 0.5,  "steps": 4},
    "std":  {"scale": 0.75, "steps": 6},
    "hq":   {"scale": 1.0,  "steps": 8},
}
CHUNK_LEN = 401   # 4k+1
CHUNK_STEP = 389  # 12프레임 겹침

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

def stream_frames(path, W, H, sample_every=1, stop_after=None):
    """전체 프레임을 한 번만 해독해 순서대로 내보내는 발생기(단일 해독 재사용용)"""
    vf = None
    if sample_every > 1: vf = f"select='not(mod(n\\,{sample_every}))'"
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if vf: cmd += ["-vf", vf]
    cmd += ["-vsync", "0", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    fsz = W * H * 3
    n = 0
    try:
        while True:
            buf = p.stdout.read(fsz)
            if not buf or len(buf) < fsz: break
            yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
            n += 1
            if stop_after is not None and n >= stop_after:
                p.terminate()
                break
    finally:
        try: p.stdout.close()
        except Exception: pass
        p.wait()

def read_all_crops(path, W, H, regions):
    """단일 해독으로 여러 영역의 잘라낸 프레임을 동시에 수집. regions: [{'x','y','w','h'}] → {ri: [frames]}"""
    out = {ri: [] for ri in range(len(regions))}
    for fr in stream_frames(path, W, H):
        for ri, r in enumerate(regions):
            out[ri].append(fr[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]].copy())
    return out

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
        if sig < max(4, 0.06 * area):
            # 보조 조건: 테두리 없는 흰 글자 — 주변이 글자보다 충분히 어두우면 인정
            md = cv2.dilate(m.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=3).astype(bool)
            ring = md & ~m
            if not ring.any(): continue
            gmean = float(mx[m].mean()); rmean = float(mx[ring].mean())
            if not (gmean - rmean >= 60 and rmean < 150): continue
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
        if (x1 - x0) < 0.45 * w and (x0 < 0.07 * w or x1 > 0.93 * w): continue
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

# ---------------- 멀티코어 감지 (결과는 순차 버전과 동일) ----------------
def _scan_boxes(f):
    """Pool용: 프레임 하나에서 자막 줄 상자(y0,y1)만 추출"""
    return [(c["y0"], c["y1"]) for c in glyph_clusters(f)]

def _mask_block(args):
    """Pool용: 겹침 포함 블록에서 핵심 구간의 원시 마스크 생성 → 비트로 압축해 반환"""
    frames, lo, hi, gstart, N = args   # frames: 블록(±6 겹침 포함), [lo,hi): 핵심 구간(블록 내 상대)
    n = len(frames)
    per = [glyph_clusters(f) for f in frames]
    h, w = frames[0].shape[:2]
    raws = []
    masked = 0
    for i in range(lo, hi):
        # 전역 기준 ±6 창과 동일해지도록: 블록 경계는 영상 경계일 때만 잘림
        keep = [c for c in per[i] if _stable_local(per, i, c, n, gstart, N)]
        if keep:
            raws.append(rasterize(keep, w, h)); masked += 1
        else:
            raws.append(np.zeros((h, w), np.uint8))
    packed = np.packbits(np.stack(raws) > 20)
    return packed, hi - lo, masked, h, w

def _stable_local(per, i, box, n, gstart, N):
    gi = gstart + i  # 전역 프레임 번호
    lo_g = max(0, gi - 6); hi_g = min(N - 1, gi + 6)
    cnt = 0
    for gj in range(lo_g, hi_g + 1):
        if gj == gi: continue
        j = gj - gstart
        if j < 0 or j >= n: continue  # (겹침이 6이면 발생하지 않음)
        if any(iou(box, b) > 0.3 for b in per[j]): cnt += 1
    return cnt >= 5

def build_subtitle_masks_par(frames):
    """build_subtitle_masks와 동일 결과를 멀티코어로 계산"""
    N = len(frames)
    if N == 0: return [], 0
    B = 240
    jobs = []
    s = 0
    while s < N:
        e = min(N, s + B)                     # 핵심 [s,e)
        bs = max(0, s - 6); be = min(N, e + 6)  # 겹침 포함
        jobs.append((frames[bs:be], s - bs, e - bs, bs, N))
        s = e
    h, w = frames[0].shape[:2]
    raw = []
    masked = 0
    with Pool(NPROC) as pool:
        for packed, cnt, mk, hh, ww in pool.imap(_mask_block, jobs):
            arr = np.unpackbits(packed, count=cnt * hh * ww).reshape(cnt, hh, ww).astype(np.uint8) * 255
            for i in range(cnt): raw.append(arr[i])
            masked += mk
    out = []
    for i in range(N):
        u = np.zeros((h, w), np.uint8)
        for j in range(max(0, i - 6), min(N - 1, i + 6) + 1):
            u |= raw[j]
        out.append(u)
    return out, masked

def detect_sub_bands_from(samples, W, H):
    """미리 읽어둔 샘플 프레임에서 자막 y-밴드 찾기 (멀티코어)"""
    hits = []
    with Pool(NPROC) as pool:
        for boxes in pool.imap(_scan_boxes, samples, chunksize=8):
            hits.extend(boxes)
    return _bands_from_hits(hits, W, H)

def _bands_from_hits(hits, W, H):
    if len(hits) < 4: return []
    groups = []
    for a, b in sorted(hits, key=lambda t: (t[0] + t[1]) / 2):
        yc = (a + b) / 2
        put = None
        for g in groups:
            if abs(g["yc"] - yc) < 90: put = g; break
        if put:
            put["items"].append((a, b))
            put["yc"] = sum((x + y) / 2 for x, y in put["items"]) / len(put["items"])
        else:
            groups.append({"yc": yc, "items": [(a, b)]})
    groups = [g for g in groups if len(g["items"]) >= 4]
    groups.sort(key=lambda g: -len(g["items"]))
    bands = []
    for g in groups[:3]:
        top = max(0, int(min(a for a, b in g["items"]) - 40))
        bot = min(H, int(max(b for a, b in g["items"]) + 40))
        bh = floor16(bot - top)
        if bh < 48: bh = 48
        bw = floor16(W)
        bx = (W - bw) // 2
        by = clamp(top, 0, H - bh)
        if any(not (by + bh <= b2["y"] or b2["y"] + b2["h"] <= by) for b2 in bands): continue
        bands.append({"x": bx, "y": by, "w": bw, "h": bh, "kind": "subtitle" + str(len(bands))})
    return bands

def detect_corner_from(samples, W, H, side):
    """미리 읽은 샘플(약 48프레임 간격)로 모서리 고정 무늬 감지 — 게이트는 기존과 동일"""
    cw = floor16(int(W * 0.42)); ch = floor16(int(H * 0.28))
    cx = 0 if side == "tl" else W - cw
    crops = [f[0:ch, cx:cx + cw] for f in samples[::4][:30]]
    if len(crops) < 6: return None
    gray = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in crops])
    mx = gray.max(0).astype(np.int32); mn = gray.min(0).astype(np.int32)
    static = (mx - mn) <= 20
    if static.mean() > 0.55: return None
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

def detect_sub_bands(work, W, H, N):
    """전체 프레임에서 안정적인 자막 줄 y-밴드를 최대 3개 찾기 (12프레임 간격 샘플)"""
    frames = read_region_frames(work, 0, 0, W, H, sample_every=12)
    hits = []
    for f in frames:
        for c in glyph_clusters(f):
            hits.append((c["y0"], c["y1"]))
    if len(hits) < 4: return []
    # y-중심을 ±90px 그룹으로 묶기
    groups = []
    for a, b in sorted(hits, key=lambda t: (t[0] + t[1]) / 2):
        yc = (a + b) / 2
        put = None
        for g in groups:
            if abs(g["yc"] - yc) < 90: put = g; break
        if put:
            put["items"].append((a, b))
            put["yc"] = sum((x + y) / 2 for x, y in put["items"]) / len(put["items"])
        else:
            groups.append({"yc": yc, "items": [(a, b)]})
    groups = [g for g in groups if len(g["items"]) >= 4]
    groups.sort(key=lambda g: -len(g["items"]))
    bands = []
    for g in groups[:3]:
        top = max(0, int(min(a for a, b in g["items"]) - 40))
        bot = min(H, int(max(b for a, b in g["items"]) + 40))
        bh = floor16(bot - top)
        if bh < 48: bh = 48
        bw = floor16(W)
        bx = (W - bw) // 2
        by = clamp(top, 0, H - bh)
        # 겹치는 밴드는 합치지 않고 건너뜀
        if any(not (by + bh <= b2["y"] or b2["y"] + b2["h"] <= by) for b2 in bands): continue
        bands.append({"x": bx, "y": by, "w": bw, "h": bh, "kind": "subtitle" + str(len(bands))})
    return bands

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

# ---------------- 병렬용 임시 저장소 (videos-clips/wmtmp) ----------------
import zlib, io as _io

def tmp_upload(path_in_bucket, data_bytes, ctype="application/octet-stream"):
    r = requests.post(f"{SB_URL}/storage/v1/object/videos-clips/{path_in_bucket}",
                      headers=sb_headers({"Content-Type": ctype, "x-upsert": "true"}),
                      data=data_bytes, timeout=600)
    r.raise_for_status()

def tmp_download(path_in_bucket):
    r = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{path_in_bucket}",
                     headers=sb_headers(), timeout=600)
    r.raise_for_status()
    return r.content

def tmp_delete(prefix, names):
    try:
        requests.delete(f"{SB_URL}/storage/v1/object/videos-clips",
                        headers=sb_headers({"Content-Type": "application/json"}),
                        data=json.dumps({"prefixes": [prefix + "/" + n for n in names]}), timeout=60)
    except Exception:
        pass

def masks_pack(masks):
    """마스크 목록(N,h,w u8 0/255) → 압축 바이트"""
    N = len(masks); h, w = masks[0].shape
    bits = np.packbits(np.stack(masks) > 20)
    raw = N.to_bytes(4, "big") + h.to_bytes(4, "big") + w.to_bytes(4, "big") + bits.tobytes()
    return zlib.compress(raw, 6)

def masks_unpack(data):
    raw = zlib.decompress(data)
    N = int.from_bytes(raw[0:4], "big"); h = int.from_bytes(raw[4:8], "big"); w = int.from_bytes(raw[8:12], "big")
    bits = np.frombuffer(raw[12:], np.uint8)
    arr = np.unpackbits(bits, count=N * h * w).reshape(N, h, w).astype(np.uint8) * 255
    return [arr[i] for i in range(N)]

def encode_chunk_mp4(frames_arr, fps, path):
    """(n,h,w,3) uint8 → 고품질 mp4"""
    n, h, w = frames_arr.shape[0], frames_arr.shape[1], frames_arr.shape[2]
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                          "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                          "-c:v", "libx264", "-crf", "10", "-preset", "veryfast", "-pix_fmt", "yuv420p", path, "-y"],
                         stdin=subprocess.PIPE)
    p.stdin.write(frames_arr.tobytes()); p.stdin.close(); p.wait()
    if p.returncode != 0: raise RuntimeError("조각 인코딩 실패")

def decode_chunk_mp4(path, w, h):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vsync", "0",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    frames = []
    fsz = w * h * 3
    while True:
        buf = p.stdout.read(fsz)
        if not buf or len(buf) < fsz: break
        frames.append(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
    p.wait()
    return np.stack(frames)

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

def plan_text_chunks(masks, N):
    """글자가 실제로 있는 프레임 구간만 4k+1 조각으로 계획 (겹침 12)"""
    txt = [i for i in range(N) if masks[i].any()]
    if not txt: return []
    ivs = []; s0 = txt[0]; p = txt[0]
    for i in txt[1:]:
        if i - p <= 24: p = i; continue
        ivs.append((s0, p)); s0 = i; p = i
    ivs.append((s0, p))
    chunks = []
    for a, b in ivs:
        a = max(0, a - 4); b = min(N - 1, b + 4)
        s = a
        while True:
            e = min(b, s + CHUNK_LEN - 1)
            if e - s + 1 < 9: s = max(0, e - 8)  # 너무 짧은 조각 방지
            n = e - s + 1
            n_use = ((n - 1) // 4) * 4 + 1
            e = min(N - 1, s + n_use - 1)
            chunks.append({"s": s, "e": e})
            if e >= b - 3: break
            s = e - 11  # 12프레임 겹침
    return chunks

def restore_chunk(frames, masks, tier, c):
    """조각 하나 AI 복원: c={'s','e'} → (n,h,w,3) uint8 (원본 해상도)"""
    import torch
    pipe = get_pipe()
    h, w = frames[0].shape[:2]
    sw = snap16(w * tier["scale"]); sh = snap16(h * tier["scale"])
    n_use = c["e"] - c["s"] + 1  # 4k+1 보장됨
    imgs = np.stack(frames[c["s"]:c["s"] + n_use]).astype(np.float32) / 127.5 - 1.0
    mks = (np.stack(masks[c["s"]:c["s"] + n_use]) > 20).astype(np.float32)[..., None]
    result = pipe(images=torch.from_numpy(imgs), masks=torch.from_numpy(mks),
                  num_frames=n_use, height=sh, width=sw,
                  num_inference_steps=tier["steps"],
                  generator=torch.Generator(device="cuda:0").manual_seed(42),
                  iterations=6).frames[0]
    arr = (np.clip(result, 0, 1) * 255).astype(np.uint8)
    if (sh, sw) != (h, w):
        arr = np.stack([cv2.resize(fr, (w, h), interpolation=cv2.INTER_LANCZOS4) for fr in arr])
    return arr

def merge_chunks_into(merged, outs):
    """outs: [{'s','e','arr'}] (s 오름차순) → merged에 겹침 중앙 기준으로 기록"""
    written_to = -1
    for o in sorted(outs, key=lambda x: x["s"]):
        s_use = o["s"]
        if o["s"] <= written_to:
            s_use = max(o["s"], written_to - 5)
        merged[s_use:o["e"] + 1] = o["arr"][s_use - o["s"]:]
        written_to = max(written_to, o["e"])
    return merged

def restore_region(frames, masks, tier, on_step=None):
    """단독(비병렬) 경로: 글자 구간만 복원"""
    N = len(frames)
    chunks = plan_text_chunks(masks, N)
    merged = np.stack(frames)
    outs = []
    for ci, c in enumerate(chunks):
        arr = restore_chunk(frames, masks, tier, c)
        outs.append({"s": c["s"], "e": c["e"], "arr": arr})
        if on_step: on_step(ci + 1, len(chunks))
    return merge_chunks_into(merged, outs)

# ---------------- 공통 준비 ----------------
def download_to(url, dest):
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024): f.write(chunk)

def fetch_lite(proj, tmp, plan):
    """plan 이후 단계용: 원본만 내려받고 계획서의 값(N 등)을 재사용 — 재검사·재인코딩 없음"""
    src = os.path.join(tmp, "src.mp4")
    download_to(signed_url(proj["source_path"], 21600), src)
    work = src
    if plan.get("cfr"):
        work = os.path.join(tmp, "work.mp4")
        with open(work, "wb") as f:
            f.write(tmp_download(f"wmtmp/{proj['id']}/work.mp4"))
    return src, work

def ownership(region_chunks):
    """merge_chunks_into와 동일한 결과가 되는 '프레임 소유권' 계산 (조각별 담당 구간)"""
    cs = sorted(region_chunks, key=lambda c: c["s"])
    su = []; written = -1
    for c in cs:
        s_use = c["s"] if c["s"] > written else max(c["s"], written - 5)
        su.append(s_use); written = max(written, c["e"])
    own = []
    for i, c in enumerate(cs):
        end = c["e"] if i == len(cs) - 1 else min(c["e"], su[i + 1] - 1)
        if end >= su[i]:
            own.append({"s": su[i], "e": end, "c": c})
    return own

def fetch_source(proj, tmp):
    src = os.path.join(tmp, "src.mp4")
    url = signed_url(proj["source_path"], 21600)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(src, "wb") as f:
            for chunk in r.iter_content(1024 * 1024): f.write(chunk)
    info = probe_info(src)
    if info["dur"] > 900: raise RuntimeError("지금은 15분 이하 영상만 지원해요. 나눠서 올려주세요.")
    work = src
    N = frame_count(work)
    expected = round(info["dur"] * info["fps"])
    if N and abs(N - expected) > max(5, expected * 0.02):
        work = os.path.join(tmp, "work.mp4")
        run(["ffmpeg", "-v", "error", "-i", src, "-vf", f"fps={info['fps']}", "-an",
             "-c:v", "libx264", "-crf", "12", "-preset", "ultrafast", "-pix_fmt", "yuv420p", work, "-y"])
        N = frame_count(work)
    return src, work, info, N

def detect_regions(proj, work, info, N, mode):
    W, H = info["W"], info["H"]
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
        regions.extend(detect_sub_bands(work, W, H, N))
        for side in ("tl", "tr"):
            c = detect_corner(work, W, H, side, N)
            if c: regions.append(c)
    return regions

def build_region_masks(pid, work, reg, N, frames=None):
    """영역 마스크 목록 생성 (frames 미리 있으면 재사용)"""
    if frames is None:
        frames = read_region_frames(work, reg["x"], reg["y"], reg["w"], reg["h"])
    n = len(frames)
    if reg["kind"].startswith("subtitle"):
        masks, masked = build_subtitle_masks(frames, n)
        if masked == 0: return frames, None
    elif reg["kind"].startswith("manual"):
        masks, masked = build_subtitle_masks(frames, n)
        if masked < max(10, math.ceil(n * 0.03)):
            m = np.zeros((reg["h"], reg["w"]), np.uint8)
            pad = 12
            m[max(0, reg["ry0"] - pad):min(reg["h"], reg["ry1"] + pad + 1),
              max(0, reg["rx0"] - pad):min(reg["w"], reg["rx1"] + pad + 1)] = 255
            masks = [m] * n
    else:
        masks = [reg["static_mask"]] * n
    return frames, masks

# ---------------- 단계: 계획 (v2: 단일 해독 + 멀티코어) ----------------
def phase_plan(proj, tmp):
    pid = proj["id"]
    sw = SW()
    set_proj(pid, "wm_running", "영상을 받아 오는 중…")
    src, work, info, N = fetch_source(proj, tmp)
    sw.mark("dl_cnt")
    cfr = (work != src)
    if cfr:
        with open(work, "rb") as f:
            tmp_upload(f"wmtmp/{pid}/work.mp4", f.read(), "video/mp4")
        sw.mark("cfr_up")
    W, H = info["W"], info["H"]
    mode = "manual" if proj.get("wm_mode") == "manual" else "auto"
    regions = []
    if mode == "manual":
        regions = detect_regions(proj, work, info, N, "manual")
    else:
        set_proj(pid, "wm_running", "자막·워터마크를 찾는 중…")
        samples = list(stream_frames(work, W, H, sample_every=12))
        sw.mark("scan_dec")
        regions.extend(detect_sub_bands_from(samples, W, H))
        for side in ("tl", "tr"):
            c = detect_corner_from(samples, W, H, side)
            if c: regions.append(c)
        del samples
        sw.mark("scan")
    if not regions:
        set_proj(pid, "wm_done", {"note": "no_target",
            "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
        return {"note": "no_target"}
    set_proj(pid, "wm_running", "자막 글자 위치를 정밀하게 잡는 중…")
    crops = read_all_crops(work, W, H, regions)   # 단일 해독으로 전 영역 수집
    if crops and crops.get(0) is not None and len(crops[0]) and len(crops[0]) != N:
        N = len(crops[0])   # 실제 해독 프레임 수 기준으로 통일 (안전장치)
    sw.mark("mask_dec")
    plan_regions = []
    all_chunks = []
    for ri, reg in enumerate(regions):
        frames = crops[ri]
        n = len(frames)
        if reg["kind"].startswith("subtitle"):
            masks, masked = build_subtitle_masks_par(frames)
            if masked == 0: masks = None
        elif reg["kind"].startswith("manual"):
            masks, masked = build_subtitle_masks_par(frames)
            if masked < max(10, math.ceil(n * 0.03)):
                m = np.zeros((reg["h"], reg["w"]), np.uint8)
                pad = 12
                m[max(0, reg["ry0"] - pad):min(reg["h"], reg["ry1"] + pad + 1),
                  max(0, reg["rx0"] - pad):min(reg["w"], reg["rx1"] + pad + 1)] = 255
                masks = [m] * n
        else:
            masks = [reg["static_mask"]] * n
        crops[ri] = None
        del frames
        if masks is None: continue
        tmp_upload(f"wmtmp/{pid}/m{len(plan_regions)}.bin", masks_pack(masks))
        chunks = plan_text_chunks(masks, N)
        for c in chunks: all_chunks.append({"r": len(plan_regions), "s": c["s"], "e": c["e"]})
        reg2 = {k: v for k, v in reg.items() if k != "static_mask"}
        plan_regions.append(reg2)
        del masks
    sw.mark("masks")
    if not plan_regions or not all_chunks:
        set_proj(pid, "wm_done", {"note": "no_target",
            "msg": "지울 자막을 찾지 못했어요. [직접 지정] 모드를 써주세요."})
        return {"note": "no_target"}
    plan = {"W": W, "H": H, "fps": info["fps"], "N": N, "audio": info["audio"],
            "mode": mode, "tier": proj.get("wm_tier") or "std", "cfr": cfr,
            "regions": plan_regions, "chunks": all_chunks}
    tmp_upload(f"wmtmp/{pid}/plan.json", json.dumps(plan).encode(), "application/json")
    sw.mark("plan_up")
    return {"phase": "plan", "chunks": len(all_chunks), "regions": len(plan_regions), "tms": sw.out()}

# ---------------- 단계: 작업 (part k / parts) — v2: 이어진 구간 배정 + 단일 해독 ----------------
def phase_work(proj, tmp, part, parts):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(f"wmtmp/{pid}/plan.json"))
    tier = TIERS.get(plan["tier"], TIERS["std"])
    total = len(plan["chunks"])
    lo = part * total // parts; hi = (part + 1) * total // parts
    my_chunks = plan["chunks"][lo:hi]
    if not my_chunks: return {"phase": "work", "part": part, "done": 0, "tms": sw.out()}
    src, work = fetch_lite(proj, tmp, plan)
    sw.mark("dl")
    need_ris = sorted(set(c["r"] for c in my_chunks))
    need_regions = [plan["regions"][ri] for ri in need_ris]
    crops = read_all_crops(work, plan["W"], plan["H"], need_regions)  # 단일 해독
    frames_by_ri = {ri: crops[k] for k, ri in enumerate(need_ris)}
    sw.mark("dec")
    masks_by_ri = {ri: masks_unpack(tmp_download(f"wmtmp/{pid}/m{ri}.bin")) for ri in need_ris}
    sw.mark("mask_dl")
    get_pipe()
    sw.mark("model")
    done = 0
    t_ai = 0.0; t_encup = 0.0
    for c in my_chunks:
        ri = c["r"]
        ta = time.time()
        arr = restore_chunk(frames_by_ri[ri], masks_by_ri[ri], tier, c)
        t_ai += time.time() - ta
        tb = time.time()
        out = os.path.join(tmp, f"o_{ri}_{c['s']}.mp4")
        encode_chunk_mp4(arr, plan["fps"], out)
        with open(out, "rb") as f:
            tmp_upload(f"wmtmp/{pid}/o_{ri}_{c['s']}.mp4", f.read(), "video/mp4")
        os.remove(out)
        t_encup += time.time() - tb
        done += 1
    sw.t["ai"] = round(t_ai, 1); sw.t["enc_up"] = round(t_encup, 1)
    return {"phase": "work", "part": part, "done": done, "tms": sw.out()}

# ---------------- 단계: 병합 ----------------
def phase_merge(proj, tmp, t0):
    pid = proj["id"]
    plan = json.loads(tmp_download(f"wmtmp/{pid}/plan.json"))
    W, H, fps, N = plan["W"], plan["H"], plan["fps"], plan["N"]
    set_proj(pid, "wm_running", "복원한 부분을 원본에 합치는 중…")
    src, work, info, N2 = fetch_source(proj, tmp)
    results = []
    tmp_names = ["plan.json"]
    for ri, reg in enumerate(plan["regions"]):
        frames = read_region_frames(work, reg["x"], reg["y"], reg["w"], reg["h"])
        masks = masks_unpack(tmp_download(f"wmtmp/{pid}/m{ri}.bin"))
        tmp_names.append(f"m{ri}.bin")
        merged = np.stack(frames)
        outs = []
        for c in [c for c in plan["chunks"] if c["r"] == ri]:
            name = f"o_{ri}_{c['s']}.mp4"
            data = tmp_download(f"wmtmp/{pid}/{name}")
            tmp_names.append(name)
            p = os.path.join(tmp, name)
            with open(p, "wb") as f: f.write(data)
            arr = decode_chunk_mp4(p, reg["w"], reg["h"])
            os.remove(p)
            n_expect = c["e"] - c["s"] + 1
            if len(arr) > n_expect: arr = arr[:n_expect]
            if len(arr) < n_expect: arr = np.concatenate([arr, np.repeat(arr[-1:], n_expect - len(arr), 0)])
            outs.append({"s": c["s"], "e": c["e"], "arr": arr})
        merge_chunks_into(merged, outs)
        results.append({"reg": reg, "restored": merged, "masks": masks})
        del frames, outs
    composite_and_finish(proj, src, work, info, N, results, t0, plan)
    tmp_delete(f"wmtmp/{pid}", tmp_names)
    return {"phase": "merge", "ok": True}

# ---------------- 단계: 구간 합성 (part k / parts) — v2 병렬 합치기 ----------------
def phase_mergeseg(proj, tmp, part, parts):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(f"wmtmp/{pid}/plan.json"))
    W, H, fps, N = plan["W"], plan["H"], plan["fps"], plan["N"]
    F0 = part * N // parts; F1 = (part + 1) * N // parts   # 담당 프레임 [F0, F1)
    src, work = fetch_lite(proj, tmp, plan)
    sw.mark("dl")
    # 각 영역: 소유권 계산 → 담당 구간과 겹치는 조각만 내려받아 프레임별 복원 결과 준비
    seg_rest = {}   # ri -> {frame_i: (h,w,3) uint8}
    masks_all = {}  # ri -> masks 목록
    for ri, reg in enumerate(plan["regions"]):
        rcs = [c for c in plan["chunks"] if c["r"] == ri]
        if not rcs: continue
        own = [o for o in ownership(rcs) if o["e"] >= F0 and o["s"] < F1]
        if not own: continue
        masks_all[ri] = masks_unpack(tmp_download(f"wmtmp/{pid}/m{ri}.bin"))
        rest = {}
        for o in own:
            c = o["c"]
            name = f"o_{ri}_{c['s']}.mp4"
            p = os.path.join(tmp, name)
            with open(p, "wb") as f: f.write(tmp_download(f"wmtmp/{pid}/{name}"))
            arr = decode_chunk_mp4(p, reg["w"], reg["h"])
            os.remove(p)
            n_expect = c["e"] - c["s"] + 1
            if len(arr) > n_expect: arr = arr[:n_expect]
            if len(arr) < n_expect: arr = np.concatenate([arr, np.repeat(arr[-1:], n_expect - len(arr), 0)])
            a = max(o["s"], F0); b = min(o["e"], F1 - 1)
            for i in range(a, b + 1):
                rest[i] = arr[i - c["s"]]
            del arr
        seg_rest[ri] = rest
    sw.mark("chunks")
    # 담당 구간만 합성해 조각 영상으로 인코딩 (오디오 없음)
    outp = os.path.join(tmp, f"seg_{part}.mp4")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                            "-c:v", "libx264", "-crf", "17", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                            outp, "-y"], stdin=subprocess.PIPE)
    i = 0
    for fr in stream_frames(work, W, H, stop_after=F1):
        if i >= F1: break
        if i >= F0:
            frame = fr.copy()
            for ri, rest in seg_rest.items():
                if i not in rest: continue
                reg = plan["regions"][ri]
                a = cv2.GaussianBlur(masks_all[ri][i], (0, 0), 2).astype(np.float32)[..., None] / 255.0
                sub = frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]].astype(np.float32)
                frame[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]] = \
                    np.clip(sub * (1 - a) + rest[i].astype(np.float32) * a, 0, 255).astype(np.uint8)
            enc.stdin.write(frame.tobytes())
        i += 1
    enc.stdin.close(); enc.wait()
    if enc.returncode != 0: raise RuntimeError("구간 합성 인코딩 실패")
    sw.mark("comp")
    with open(outp, "rb") as f:
        tmp_upload(f"wmtmp/{pid}/seg_{part}.mp4", f.read(), "video/mp4")
    sw.mark("up")
    return {"phase": "mergeseg", "part": part, "frames": F1 - F0, "tms": sw.out()}

# ---------------- 단계: 마무리 (이어붙이기 + 오디오 + 업로드) ----------------
def phase_finish(proj, tmp, t0, parts, tms_in=None):
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(f"wmtmp/{pid}/plan.json"))
    set_proj(pid, "wm_running", "마무리 중…")
    seg_paths = []
    tmp_names = ["plan.json"] + [f"m{ri}.bin" for ri in range(len(plan["regions"]))]
    if plan.get("cfr"): tmp_names.append("work.mp4")
    for c in plan["chunks"]: tmp_names.append(f"o_{c['r']}_{c['s']}.mp4")
    for k in range(parts):
        p = os.path.join(tmp, f"seg_{k}.mp4")
        with open(p, "wb") as f: f.write(tmp_download(f"wmtmp/{pid}/seg_{k}.mp4"))
        seg_paths.append(p)
        tmp_names.append(f"seg_{k}.mp4")
    sw.mark("dl")
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in seg_paths: f.write(f"file '{p}'\n")
    outp = os.path.join(tmp, "out.mp4")
    if plan.get("audio"):
        aurl = signed_url(proj["source_path"], 21600)
        run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-i", aurl,
             "-map", "0:v", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", outp, "-y"])
    else:
        run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
             "-c", "copy", "-movflags", "+faststart", outp, "-y"])
    sw.mark("concat")
    dest = f"{proj['user_id']}/wm_{pid}.mp4"
    url_out = upload_clip(dest, outp)
    sw.mark("up")
    ym = time.strftime("%Y-%m", time.gmtime())
    try:
        requests.post(f"{SB_URL}/rest/v1/wm_usage",
                      params={"on_conflict": "user_id,ym"},
                      headers=sb_headers({"Content-Type": "application/json",
                                          "Prefer": "resolution=ignore-duplicates,return=minimal"}),
                      data=json.dumps({"user_id": proj["user_id"], "ym": ym, "used": 0}), timeout=30)
        cur = sb_select_one("wm_usage", {"user_id": "eq." + proj["user_id"], "ym": "eq." + ym}, "id,used")
        if cur: sb_update("wm_usage", {"id": "eq." + str(cur["id"])}, {"used": (cur.get("used") or 0) + 1})
    except Exception:
        traceback.print_exc()
    sec = round(time.time() - t0)
    mode = plan.get("mode") or ("manual" if proj.get("wm_mode") == "manual" else "auto")
    tms = dict(tms_in or {})
    tms["finish"] = sw.out()
    detail = {"url": url_out, "mode": mode, "tier": plan.get("tier") or "std",
              "regions": [r["kind"] for r in plan["regions"]], "sec": sec, "gpu": "runpod", "tms": tms}
    set_proj(pid, "wm_done", detail)
    tmp_delete(f"wmtmp/{pid}", tmp_names)
    print("[gpu-wm] 완료(v2)", pid, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
    return {"phase": "finish", "ok": True, "sec": sec}

# ---------------- 합성 + 마무리 (공용) ----------------
def composite_and_finish(proj, src, work, info, N, results, t0, plan=None):
    pid = proj["id"]
    W, H, fps = info["W"], info["H"], info["fps"]
    tmpdir = os.path.dirname(src)
    outp = os.path.join(tmpdir, "out.mp4")
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
    dest = f"{proj['user_id']}/wm_{pid}.mp4"
    url_out = upload_clip(dest, outp)
    ym = time.strftime("%Y-%m", time.gmtime())
    try:
        requests.post(f"{SB_URL}/rest/v1/wm_usage",
                      params={"on_conflict": "user_id,ym"},
                      headers=sb_headers({"Content-Type": "application/json",
                                          "Prefer": "resolution=ignore-duplicates,return=minimal"}),
                      data=json.dumps({"user_id": proj["user_id"], "ym": ym, "used": 0}), timeout=30)
        cur = sb_select_one("wm_usage", {"user_id": "eq." + proj["user_id"], "ym": "eq." + ym}, "id,used")
        if cur: sb_update("wm_usage", {"id": "eq." + str(cur["id"])}, {"used": (cur.get("used") or 0) + 1})
    except Exception:
        traceback.print_exc()
    sec = round(time.time() - t0)
    mode = "manual" if proj.get("wm_mode") == "manual" else "auto"
    detail = {"url": url_out, "mode": mode, "tier": proj.get("wm_tier") or "std",
              "regions": [r["reg"]["kind"] for r in results], "sec": sec, "gpu": "runpod"}
    set_proj(pid, "wm_done", detail)
    print("[gpu-wm] 완료", pid, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
    return detail

# ---------------- 단독(비병렬) 전체 처리 ----------------
def phase_all(proj, tmp, t0):
    pid = proj["id"]
    set_proj(pid, "wm_running", "영상을 받아 오는 중…")
    src, work, info, N = fetch_source(proj, tmp)
    mode = "manual" if proj.get("wm_mode") == "manual" else "auto"
    set_proj(pid, "wm_running", "자막·워터마크를 찾는 중…")
    regions = detect_regions(proj, work, info, N, mode)
    if not regions:
        set_proj(pid, "wm_done", {"note": "no_target",
            "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
        return {"note": "no_target"}
    results = []
    for reg in regions:
        set_proj(pid, "wm_running", "자막 글자 위치를 정밀하게 잡는 중…")
        frames, masks = build_region_masks(pid, work, reg, N)
        if masks is None: continue
        set_proj(pid, "wm_running", "AI가 배경을 복원하는 중…")
        def on_step(d, t):
            try: set_proj(pid, "wm_running", f"AI가 배경을 복원하는 중… ({d}/{t} 조각)")
            except Exception: pass
        merged = restore_region(frames, masks, TIERS.get(proj.get("wm_tier") or "std", TIERS["std"]), on_step)
        results.append({"reg": reg, "restored": merged, "masks": masks})
    if not results:
        set_proj(pid, "wm_done", {"note": "no_target",
            "msg": "지울 자막을 찾지 못했어요. [직접 지정] 모드를 써주세요."})
        return {"note": "no_target"}
    set_proj(pid, "wm_running", "복원한 부분을 원본에 합치는 중…")
    return composite_and_finish(proj, src, work, info, N, results, t0)

# ---------------- 진입점 ----------------
def handler(event):
    inp = event.get("input") or {}
    pid = inp.get("project_id")
    phase = inp.get("phase") or "all"
    if not pid: return {"error": "project_id가 없습니다"}
    t0 = inp.get("t0") or time.time()
    tmp = tempfile.mkdtemp(prefix="ibwm-")
    try:
        proj = sb_select_one("sc_projects", {"id": "eq." + pid})
        if not proj: return {"error": "프로젝트를 찾을 수 없어요: " + pid}
        if phase == "plan": return phase_plan(proj, tmp)
        if phase == "work": return phase_work(proj, tmp, int(inp.get("part", 0)), int(inp.get("parts", 1)))
        if phase == "mergeseg": return phase_mergeseg(proj, tmp, int(inp.get("part", 0)), int(inp.get("parts", 1)))
        if phase == "finish": return phase_finish(proj, tmp, t0, int(inp.get("parts", 1)), inp.get("tms"))
        if phase == "merge": return phase_merge(proj, tmp, t0)
        return phase_all(proj, tmp, t0)
    except Exception as e:
        traceback.print_exc()
        try:
            if phase in ("all", "merge", "plan", "finish", "mergeseg"):
                set_proj(pid, "failed_wm", str(e)[:300])
        except Exception:
            pass
        return {"error": str(e)[:500]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": handler})
