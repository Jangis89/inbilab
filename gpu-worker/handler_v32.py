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


# ---------------- Phase 1: 병렬 다운로드 + S3 멀티파트 업로드 ----------------
def _content_length(url):
    import requests as rq
    r = rq.get(url, headers={"Range": "bytes=0-0"}, timeout=30)
    r.raise_for_status()
    cr = r.headers.get("Content-Range", "")
    if "/" in cr:
        return int(cr.split("/")[-1])
    return int(r.headers.get("Content-Length") or 0)


def frame_count_fast(path):
    """프레임 수를 '해독 없이' 패킷 수로 센다 (mp4 H.264: 패킷=프레임).
    v29의 frame_count(-count_frames, 전체 해독 ~50초)와 달리 1초 미만.
    실패 시 기존 방식으로 폴백."""
    try:
        out = h29.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                       "-count_packets", "-show_entries", "stream=nb_read_packets",
                       "-of", "csv=p=0", path]).stdout
        n = int(out.decode().strip() or 0)
        if n > 0:
            return n
    except Exception:
        pass
    return h29.frame_count(path)


def download_to_par(url, dest, conc=8, chunk_mb=16):
    """HTTP Range 병렬 다운로드 — 서명 URL 그대로 사용(키 불필요). 실패 조각은 3회 재시도."""
    import requests as rq
    from concurrent.futures import ThreadPoolExecutor
    size = _content_length(url)
    if size <= chunk_mb * 1024 * 1024:
        h29.download_to(url, dest)
        return os.path.getsize(dest)
    with open(dest, "wb") as f:
        f.truncate(size)
    cs = chunk_mb * 1024 * 1024
    ranges = [(a, min(a + cs, size) - 1) for a in range(0, size, cs)]
    def _one(rg):
        a, b = rg
        for att in range(3):
            try:
                r = rq.get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=300)
                r.raise_for_status()
                with open(dest, "r+b") as f:
                    f.seek(a); f.write(r.content)
                return
            except Exception:
                if att == 2:
                    raise
                time.sleep(1 + att)
    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(_one, ranges))
    if os.path.getsize(dest) != size:
        raise RuntimeError("[v32] 병렬 다운로드 크기 불일치")
    return size


def fetch_source_fast(proj, tmp, sw=None):
    """h29.fetch_source와 동일 결과 — 원본 다운로드만 Range 병렬 + 단계 분해 계측."""
    src = os.path.join(tmp, "src.mp4")
    url = h29.signed_url(proj["source_path"], 21600)
    download_to_par(url, src)
    if sw: sw.mark("dl")
    info = h29.probe_info(src)
    if info["dur"] > 900:
        raise RuntimeError("지금은 15분 이하 영상만 지원해요. 나눠서 올려주세요.")
    work = src
    N = frame_count_fast(work)
    if sw: sw.mark("cnt")
    expected = round(info["dur"] * info["fps"])
    if N and abs(N - expected) > max(5, expected * 0.02):
        work = os.path.join(tmp, "work.mp4")
        h29.run(["ffmpeg", "-v", "error"] + h29.hw_dec_args()
                + ["-i", src, "-vf", f"fps={info['fps']}", "-an"]
                + h29.hw_enc_args(12) + [work, "-y"])
        N = frame_count_fast(work)
        if sw: sw.mark("cfr_enc")
    return src, work, info, N


_S3_CACHED = {"c": None, "tried": False, "ep": None}

def _s3_client():
    """Supabase S3 클라이언트. 키는 Modal Secret(v32-staging-s3)로만 주입 —
    코드·로그에 값 노출 금지. PLACEHOLDER면 비활성(기존 단일 PUT 폴백)."""
    if _S3_CACHED["tried"]:
        return _S3_CACHED["c"]
    _S3_CACHED["tried"] = True
    kid = os.environ.get("SUPABASE_S3_ACCESS_KEY_ID", "")
    sec = os.environ.get("SUPABASE_S3_SECRET_ACCESS_KEY", "")
    _S3_CACHED["diag"] = {"key_len": len(kid), "sec_len": len(sec),
                          "placeholder": ("PLACEHOLDER" in kid) or ("PLACEHOLDER" in sec),
                          "region": os.environ.get("SUPABASE_S3_REGION", "(없음)"), "errors": []}
    if not kid or not sec or "PLACEHOLDER" in kid or "PLACEHOLDER" in sec:
        _S3_CACHED["diag"]["why"] = "키 없음 또는 PLACEHOLDER"
        return None
    try:
        import boto3
        from botocore.config import Config
    except Exception as e:
        _S3_CACHED["diag"]["why"] = f"boto3 import 실패: {type(e).__name__}"
        return None
    # region은 비밀값이 아님 — 프로젝트 실측값(ap-southeast-1)을 1순위로 두고
    # 잘못 저장된 secret 값에 견디도록 후보를 순차 시도한다
    regs = []
    for rg in (os.environ.get("SUPABASE_S3_REGION"), "ap-southeast-1",
               "ap-northeast-2", "us-east-1"):
        if rg and rg not in regs:
            regs.append(rg)
    eps = [e for e in [os.environ.get("SUPABASE_S3_ENDPOINT"),
                       h29.SB_URL.replace(".supabase.co", ".storage.supabase.co") + "/storage/v1/s3",
                       h29.SB_URL + "/storage/v1/s3"] if e]
    for region in regs:
        for ep in eps:
            try:
                c = boto3.client("s3", endpoint_url=ep, aws_access_key_id=kid,
                                 aws_secret_access_key=sec, region_name=region,
                                 config=Config(s3={"addressing_style": "path"},
                                               max_pool_connections=16,
                                               retries={"max_attempts": 2},
                                               connect_timeout=10, read_timeout=180))
                c.head_bucket(Bucket="videos-clips")
                _S3_CACHED.update(c=c, ep=ep)
                _S3_CACHED["diag"]["region_used"] = region
                return c
            except Exception as e:
                _S3_CACHED["diag"]["errors"].append(
                    f"{region}@{ep.split('/')[2]}: {type(e).__name__}: {str(e)[:120]}")
                continue
    _S3_CACHED["diag"]["why"] = "모든 region/endpoint 조합 실패"
    return None


def s3_upload(bucket, key, filepath, part_mb=16, conc=8, client=None):
    """멀티파트 병렬 업로드 + 크기 검증. 시작 전 같은 key의 미완료 part 정리(abort)."""
    from boto3.s3.transfer import TransferConfig
    c = client or _s3_client()
    if c is None:
        raise RuntimeError("[v32] S3 키 없음")
    try:
        mp = c.list_multipart_uploads(Bucket=bucket, Prefix=key)
        for u in mp.get("Uploads") or []:
            c.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
    except Exception:
        pass
    cfg = TransferConfig(multipart_threshold=8 * 1024 * 1024,
                         multipart_chunksize=part_mb * 1024 * 1024,
                         max_concurrency=conc, use_threads=True)
    c.upload_file(filepath, bucket, key, Config=cfg,
                  ExtraArgs={"ContentType": "video/mp4"})
    head = c.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != os.path.getsize(filepath):
        raise RuntimeError(f"[v32] 업로드 크기 불일치 {head['ContentLength']}")
    return head


def _sign_clip(path_in_bucket):
    import requests as rq
    def _do():
        r = rq.post(f"{h29.SB_URL}/storage/v1/object/sign/videos-clips/{path_in_bucket}",
                    headers=h29.sb_headers({"Content-Type": "application/json"}),
                    data=json.dumps({"expiresIn": 86400}), timeout=30)
        r.raise_for_status()
        return h29.SB_URL + "/storage/v1" + r.json()["signedURL"]
    return h29._retry(_do)


def upload_clip_fast(path_in_bucket, filepath, sw=None):
    """S3 멀티파트(키 있으면) → 2회 실패 시 기존 단일 PUT 폴백. (서명URL, 방식) 반환."""
    part_mb = int(os.environ.get("WM_S3_PART_MB", "16"))
    conc = int(os.environ.get("WM_S3_CONC", "8"))
    if _s3_client() is not None:
        for att in range(2):
            try:
                s3_upload("videos-clips", path_in_bucket, filepath, part_mb, conc)
                if sw: sw.mark("up_s3")
                url = _sign_clip(path_in_bucket)
                if sw: sw.mark("sign")
                return url, f"s3-{part_mb}x{conc}"
            except Exception:
                time.sleep(1)
    url = h29.upload_clip(path_in_bucket, filepath)
    if sw: sw.mark("up_put")
    return url, "put"


def fetch_lite_v32(proj, tmp, plan):
    pid = proj["id"]
    src = h29.cache_get(pid + "-src.mp4")
    if not src:
        src = os.path.join(tmp, "src.mp4")
        download_to_par(h29.signed_url(proj["source_path"], 21600), src)
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


# ---------------- scan 감지 공유메모리화 (pickle 5GB 제거) ----------------
_V32_POOL = None
def _v32_scan_pool():
    global _V32_POOL
    if _V32_POOL is None:
        import multiprocessing as _mp
        _V32_POOL = _mp.get_context("spawn").Pool(h29.NPROC, initializer=v31._shm_pool_init)
    return _V32_POOL

_SCAN_CHILD = {}

def _scan_block(args):
    shm_name, shape, idxs = args
    import numpy as _np
    from multiprocessing import shared_memory as _sm
    if _SCAN_CHILD.get("name") != shm_name:
        old = _SCAN_CHILD.pop("shm", None)
        if old is not None:
            try: old.close()
            except Exception: pass
        shm = _sm.SharedMemory(name=shm_name)
        _SCAN_CHILD.update(name=shm_name, shm=shm,
                           arr=_np.ndarray(shape, dtype=_np.uint8, buffer=shm.buf))
    arr = _SCAN_CHILD["arr"]
    import handler as _h
    n = shape[0]
    out = []
    for i in idxs:
        ref = arr[i - 1] if i > 0 else (arr[1] if n > 1 else None)
        # _scan_boxes2와 동일 계산 + 글자 전체상자(items)도 수집 (transient 감지용,
        # glyph_clusters는 어차피 계산되므로 추가 비용 없음)
        cl = _h.glyph_clusters(arr[i])
        lines = [(c["y0"], c["y1"]) for c in cl]
        items = [(int(it["x0"]), int(it["y0"]), int(it["x1"]), int(it["y1"]))
                 for c in cl for it in c["items"]]
        labs = [(g["x0"], g["y0"], g["x1"], g["y1"])
                for g in _h.glyph_labels(arr[i], ref)]
        out.append((lines, labs, items))
    return out


def detect_sub_bands_shm(samples, W, H):
    """h29.detect_sub_bands_from과 동일 결과 — 샘플 전달만 공유메모리 (그룹핑은 순서 무관)."""
    global _V32_POOL
    detect_sub_bands_shm._last_items_t = None            # 폴백 시 transient 감지 생략
    n = len(samples)
    if n < 4:
        return h29.detect_sub_bands_from(samples, W, H)
    hh, ww = samples[0].shape[:2]
    from multiprocessing import shared_memory as _sm
    shm = None
    try:
        shm = _sm.SharedMemory(create=True, size=n * hh * ww * 3)
        arr = np.ndarray((n, hh, ww, 3), dtype=np.uint8, buffer=shm.buf)
        for i in range(n):
            arr[i] = samples[i]
        per_b = max(1, (n + h29.NPROC * 2 - 1) // (h29.NPROC * 2))
        jobs = [(shm.name, (n, hh, ww, 3), list(range(a, min(n, a + per_b))))
                for a in range(0, n, per_b)]
        parts = _v32_scan_pool().map_async(_scan_block, jobs).get(timeout=600)
        hits = []; labs = []; items_t = []
        for pt in parts:
            for boxes, lb, items in pt:
                hits.extend(boxes); labs.extend(lb); items_t.append(items)
        detect_sub_bands_shm._last_items_t = items_t     # transient 감지가 재사용
        return h29._bands_from_hits(hits, W, H) + h29._label_regions_from(labs, W, H)
    except Exception:
        try:
            if _V32_POOL is not None: _V32_POOL.terminate()
        except Exception:
            pass
        _V32_POOL = None
        return h29.detect_sub_bands_from(samples, W, H)
    finally:
        if shm is not None:
            try: shm.close(); shm.unlink()
            except Exception: pass


def detect_static_overlays(samples, W, H):
    """전 구간 고정 오버레이 감지 (Phase 2 UAT hotfix — 자막 밴드가 놓치는
    고정 자막 획·워터마크·불투명 카드). corner 감지와 동일 원리를 전면으로 확장:
    시간축 max-min≤22(정적) & 시간축 median gradient>14(무늬).
    글자 성분들을 인접 병합하고, 채움률 낮은 카드형은 rect 전체를 마스크로.
    반환: static_mask region 목록 (최대 3개, 총면적 프레임 10% 한도)."""
    sub = samples[::3][:60]
    if len(sub) < 8:
        return []
    g = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2GRAY) for f in sub])
    mx = g.max(0).astype(np.int32); mn = g.min(0).astype(np.int32)
    static = (mx - mn) <= 22
    if static.mean() > 0.5:
        return []                      # 전면 정지 영상 — 신뢰 불가
    med = np.median(g, 0).astype(np.int32)
    gx = np.abs(np.diff(med, axis=1)); gx = np.pad(gx, ((0, 0), (0, 1)))
    gy = np.abs(np.diff(med, axis=0)); gy = np.pad(gy, ((0, 1), (0, 0)))
    sig = (static & ((gx + gy) > 14)).astype(np.uint8)
    d = cv2.dilate(sig, np.ones((3, 3), np.uint8), iterations=6)
    ncc, lab, stats, _c = cv2.connectedComponentsWithStats(d)
    boxes = []
    for i in range(1, ncc):
        x, y, w2, h2, area = stats[i]
        if area < 300 or area > W * H * 0.10:
            continue
        boxes.append([int(x), int(y), int(x + w2), int(y + h2)])
    # 인접 성분 병합 (가로 글자열·카드 글자 묶음)
    merged = True
    while merged and len(boxes) > 1:
        merged = False
        out = []
        while boxes:
            b = boxes.pop()
            j = 0
            while j < len(boxes):
                o = boxes[j]
                if not (b[2] + 48 < o[0] or o[2] + 48 < b[0]
                        or b[3] + 48 < o[1] or o[3] + 48 < b[1]):
                    b = [min(b[0], o[0]), min(b[1], o[1]),
                         max(b[2], o[2]), max(b[3], o[3])]
                    boxes.pop(j); merged = True
                else:
                    j += 1
            out.append(b)
        boxes = out
    # 어두운 반투명 박스 위 글자는 제외 — 그 영역은 box un-blend 경로가 담당
    # (안쪽 링(+16px)이 바깥 링(+16~48px)보다 8 이상 어두우면 반투명 박스 위로 판정)
    def _ring_vals(b, a, c):
        x0, y0, x1, y1 = b
        m = np.zeros(med.shape, bool)
        m[max(0, y0 - c):min(H, y1 + c), max(0, x0 - c):min(W, x1 + c)] = True
        m[max(0, y0 - a):min(H, y1 + a), max(0, x0 - a):min(W, x1 + a)] = False
        return med[m]
    kept = []
    for b in boxes:
        inner = _ring_vals(b, 0, 16)
        outer = _ring_vals(b, 16, 48)
        if inner.size >= 50 and outer.size >= 50 \
                and float(np.mean(outer)) - float(np.mean(inner)) >= 8.0:
            continue
        kept.append(b)
    boxes = kept
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    regs = []
    total_px = 0
    for b in boxes[:3]:
        x0, y0, x1, y1 = b
        gx0 = max(0, x0 - 24); gy0 = max(0, y0 - 24)
        gx1 = min(W, x1 + 24); gy1 = min(H, y1 + 24)
        gw = (gx1 - gx0) // 16 * 16
        gh = (gy1 - gy0) // 16 * 16
        if gw < 48: gw = 48
        if gh < 48: gh = 48
        gx0 = max(0, min(gx0, W - gw)); gy0 = max(0, min(gy0, H - gh))
        if total_px + gw * gh > W * H * 0.10:
            continue
        total_px += gw * gh
        crop_d = d[gy0:gy0 + gh, gx0:gx0 + gw]
        crop_static = static[gy0:gy0 + gh, gx0:gx0 + gw]
        fill = float(crop_d.mean())
        if fill < 0.5 and float(crop_static.mean()) > 0.8:
            # 카드형(불투명 균일면 위 글자): rect 전체 복원
            m = np.full((gh, gw), 255, np.uint8)
        else:
            m = (cv2.dilate(crop_d, np.ones((3, 3), np.uint8), iterations=3) * 255
                 ).astype(np.uint8)
        regs.append({"x": int(gx0), "y": int(gy0), "w": int(gw), "h": int(gh),
                     "kind": "static" + str(len(regs)), "static_mask": m})
    return regs


def _scene_text_veto(regions, items_t, samples, W, H):
    """실물(장면 부착) 텍스트 보호 (RC2 Phase D).
    항목별 최근접 매칭: 연속 표본에서 같은 글자를 짝지어 이동량을 재고,
    전역 배경 이동과 같이 움직인 짝(scene)과 화면 고정 짝(fixed)을 센다.
    scene 비율이 높으면 그 영역은 실물 텍스트 — 계획에서 제외.
    (중앙값 추적은 실물+기타가 섞이면 희석돼 실패 — run103 g21 corr 0.62 실측)"""
    if not items_t or len(samples) < 16:
        return regions
    n = len(samples)
    sc = 256.0 / max(W, H)
    sw2, sh2 = max(32, int(W * sc) // 2 * 2), max(32, int(H * sc) // 2 * 2)
    gq = [cv2.resize(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY), (sw2, sh2),
                     interpolation=cv2.INTER_AREA).astype(np.float32) for f in samples]
    win = cv2.createHanningWindow((sw2, sh2), cv2.CV_32F)
    gdx = np.zeros(n); gdy = np.zeros(n)
    for i in range(1, n):
        (dx, dy), _resp = cv2.phaseCorrelate(gq[i - 1], gq[i], win)
        gdx[i] = dx / sc; gdy[i] = dy / sc        # 전역 배경(콘텐츠) 이동 (원본 px)
    move_total = float(np.sum(np.hypot(gdx, gdy)))
    out = []
    dbg = []
    _scene_text_veto._last_dbg = dbg
    cents = [[((b2[0] + b2[2]) / 2.0, (b2[1] + b2[3]) / 2.0) for b2 in (its or [])]
             for its in items_t]
    for reg in regions:
        kind = str(reg.get("kind", ""))
        if not (kind.startswith("subtitle") or kind.startswith("label")
                or kind.startswith("static")):
            out.append(reg); continue
        if move_total < 60.0:
            dbg.append({"k": kind, "why": "move<60", "move": round(move_total, 1)})
            out.append(reg); continue
        rx0, ry0 = reg["x"], reg["y"]
        rx1, ry1 = reg["x"] + reg["w"], reg["y"] + reg["h"]
        scene_n = 0; fixed_n = 0
        for i in range(1, n):
            g = (gdx[i], gdy[i]); gm = float(np.hypot(*g))
            if gm <= 1.5 or gm > 60.0:
                continue    # 정지 표본은 정보 없음, 60px+는 장면전환 잡음 (오판 방지)
            cur = [c for c in cents[i] if rx0 <= c[0] <= rx1 and ry0 <= c[1] <= ry1]
            prev = cents[i - 1]
            if not cur or not prev:
                continue
            for cx, cy in cur:
                best = None; bd = 1e9
                for px, py in prev:
                    d2 = (cx - px) ** 2 + (cy - py) ** 2
                    if d2 < bd:
                        bd = d2; best = (px, py)
                if best is None or bd > 120.0 ** 2:
                    continue
                ddx = cx - best[0]; ddy = cy - best[1]
                err_scene = float(np.hypot(ddx - g[0], ddy - g[1]))
                mag = float(np.hypot(ddx, ddy))
                if err_scene < max(4.0, 0.35 * gm):
                    scene_n += 1
                elif mag < max(3.0, 0.25 * gm):
                    fixed_n += 1
        tot = scene_n + fixed_n
        # 판정 규칙 (실측 보정):
        #  (a) fixed 짝이 하나도 없는 순수 scene 텍스트는 15쌍이면 충분히 확실
        #      (실물 골든 g15-보조/g21/g22/g24: 16~309쌍 전부 fixed=0)
        #  (b) 섞인 경우는 총 40쌍 이상 + scene 비율 70% 요구 — 빠른 컷 영상에서
        #      얇은 표본(27쌍, fixed 6)으로 실제 자막을 오판한 실측(run111) 방지
        ratio = scene_n / float(tot) if tot else 0.0
        if (scene_n >= 15 and fixed_n == 0) or \
                (scene_n >= 8 and tot >= 40 and ratio >= 0.7):
            dbg.append({"k": kind, "why": "VETO", "scene": scene_n, "fixed": fixed_n,
                        "move": round(move_total, 1)})
            continue                              # 장면 부착 텍스트 — 영역 제외
        dbg.append({"k": kind, "why": "keep", "scene": scene_n, "fixed": fixed_n,
                    "move": round(move_total, 1)})
        out.append(reg)
    return out


def detect_windowed_transient_overlays(samples, W, H, scan_step, fps,
                                       prior_regions=None, items_t=None):
    """구간별로 잠깐 나타나는 반투명 오버레이 감지 (RC2 Phase C).
    scan에서 이미 계산한 프레임별 글자 상자(items_t)를 시간·공간으로 묶어
    기존 밴드·정적 감지가 놓치는 두 유형을 찾는다:
      Type A(반투명 카드): 카드 rect를 포함하는 영역을 표준 경로로 내보내
        기존 box 감지(un-blend/AI)가 박스째 처리하게 한다.
      Type B(반투명 워터마크): 프레임별 감지가 끊기는(반투명 저대비) 글자를
        시간축 누적 마스크(static_pack)+등장구간(ivals)으로 내보낸다 —
        단일 프레임 glyph=0이어도 구간 증거로 제거 (명세 C.3).
    구간(ivals)은 원해상도 crop의 edge 지속성 타임라인 + hysteresis로 정한다.
    실물 오탐 방지: 장면 안(in-shot) 등장·퇴장 증거 또는 복수 구간 재등장 필수,
    glyph 증거 필수, 저신뢰 대면적 mask 금지 (명세 D)."""
    n = len(samples)
    if n < 12 or fps <= 0 or not items_t:
        return []
    spf = scan_step / float(fps)                 # 샘플 1개당 초
    # ---------- 1) 글자 지속성 히트맵 → 후보 클러스터 ----------
    # 프레임별 글자 상자를 1/8 해상도 히트맵에 누적. '같은 자리에 글자가
    # 오래 머무는' 픽셀(≥15% 샘플)만 남긴다 — 일반 자막은 글자가 계속
    # 바뀌므로 픽셀 지속성이 낮아(≈0.07) 자동으로 걸러진다 (UAT-02 실측:
    # 카드 0.72 / 巴图 0.24 / 자막 밴드 p90 0.07).
    S8 = 8
    hw8, hh8 = max(8, W // S8), max(8, H // S8)
    acc = np.zeros((hh8, hw8), np.int32)
    any_items = False
    for its in items_t:
        if not its:
            continue
        any_items = True
        m8 = np.zeros((hh8, hw8), bool)
        for x0, y0, x1, y1 in its:
            m8[y0 // S8:(y1 // S8) + 1, x0 // S8:(x1 // S8) + 1] = True
        acc += m8
    if not any_items:
        return []
    frac8 = acc / float(n)
    cand8 = (frac8 >= 0.15).astype(np.uint8)
    if not cand8.any():
        return []
    cand8 = cv2.dilate(cand8, np.ones((3, 3), np.uint8), iterations=2)
    ncc, lab8, stats8, _c8 = cv2.connectedComponentsWithStats(cand8)
    cboxes = []
    for ci in range(1, ncc):
        x, y, w2, h2, area = stats8[ci]
        if area < 20 or area > hw8 * hh8 * 0.12:
            continue
        cboxes.append([int(x * S8), int(y * S8),
                       int((x + w2) * S8), int((y + h2) * S8)])
    # 인접 병합 (가로 48px / 세로 16px — 서로 다른 오버레이가 상하로 붙는 것 방지)
    merged = True
    while merged and len(cboxes) > 1:
        merged = False
        out = []
        while cboxes:
            bb = cboxes.pop()
            j = 0
            while j < len(cboxes):
                ob = cboxes[j]
                if not (bb[2] + 48 < ob[0] or ob[2] + 48 < bb[0]
                        or bb[3] + 16 < ob[1] or ob[3] + 16 < bb[1]):
                    bb = [min(bb[0], ob[0]), min(bb[1], ob[1]),
                          max(bb[2], ob[2]), max(bb[3], ob[3])]
                    cboxes.pop(j); merged = True
                else:
                    j += 1
            out.append(bb)
        cboxes = out
    clusters = []
    for bb in cboxes:
        hits = set()
        for si, its in enumerate(items_t):
            for x0, y0, x1, y1 in its or []:
                if x0 < bb[2] and x1 > bb[0] and y0 < bb[3] and y1 > bb[1]:
                    hits.add(si); break
        clusters.append({"box": bb, "hits": hits})
    # 장면전환 (in-shot 판정용, 저해상도)
    sc = 384.0 / max(W, H)
    gq = np.stack([cv2.cvtColor(cv2.resize(f, (int(W * sc), int(H * sc)),
                   interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
                   for f in samples]).astype(np.int16)
    dif = np.array([0.0] + [float(np.abs(gq[i] - gq[i - 1]).mean()) for i in range(1, n)])
    cuts = set(np.where(dif > 28)[0].tolist())
    del gq
    min_hits = max(3, int(round(1.0 / spf) // 2))
    clusters = [c for c in clusters if len(c["hits"]) >= min_hits]
    clusters.sort(key=lambda c: -len(c["hits"]))
    # ---------- 2) 후보별 정밀 검증 ----------
    regs = []
    total_px = 0
    for c in clusters[:6]:
        if len(regs) >= 2:
            break
        bx0, by0, bx1, by1 = c["box"]
        # 영역 상자: 여유 24px + 16 정렬
        fx0 = max(0, bx0 - 24); fy0 = max(0, by0 - 24)
        gw = min(W, bx1 + 24) - fx0; gh = min(H, by1 + 24) - fy0
        gw = max(48, gw // 16 * 16); gh = max(48, gh // 16 * 16)
        fx0 = max(0, min(fx0, W - gw)); fy0 = max(0, min(fy0, H - gh))
        area_frac = gw * gh / float(W * H)
        if area_frac > 0.10:
            continue                             # 대면적 자동 mask 금지 (명세 D.3)
        # 등장 타임라인: 원해상도 crop edge 에너지 (작은 crop이라 저비용)
        en = np.zeros(n, np.float32)
        for i in range(n):
            g = cv2.cvtColor(samples[i][fy0:fy0 + gh, fx0:fx0 + gw],
                             cv2.COLOR_RGB2GRAY).astype(np.int16)
            gx = np.abs(np.diff(g, axis=1)).sum(); gy = np.abs(np.diff(g, axis=0)).sum()
            en[i] = (gx + gy) / float(gw * gh)
        lo, hi = float(np.percentile(en, 10)), float(np.percentile(en, 90))
        if hi - lo < 1.0:
            continue                             # 시간축 대비 없음 — 항상 유사 = 실물/정적 몫
        th_on = lo + 0.55 * (hi - lo); th_off = lo + 0.30 * (hi - lo)
        hitv = np.zeros(n, bool)
        for si in c["hits"]:
            hitv[si] = True
        ivs = []; on = False; s0 = 0; low = 0
        for i in range(n):
            if not on:
                if (en[i] >= th_on and (hitv[max(0, i - 2):i + 3].any())):
                    on = True; s0 = i; low = 0
            else:
                if en[i] < th_off:
                    low += 1
                    if low >= 3:
                        ivs.append([s0, i - low + 1]); on = False
                else:
                    low = 0
        if on:
            ivs.append([s0, n])
        gap_s = max(1, int(round(1.5 / spf))); min_s = max(2, int(round(1.0 / spf)))
        m2 = []
        for iv in ivs:
            if m2 and iv[0] - m2[-1][1] <= gap_s:
                m2[-1][1] = iv[1]
            else:
                m2.append(list(iv))
        ivs = [iv for iv in m2 if iv[1] - iv[0] >= min_s]
        if not ivs:
            continue
        pres = np.zeros(n, bool)
        for a, b in ivs:
            pres[a:b] = True
        cov = float(pres.mean())
        support = len([s for s in c["hits"] if pres[s]]) / max(1.0, float(pres.sum()))
        # 실물 오탐 방지: 장면 안 등장·퇴장 최소 1회 또는 복수 구간 재등장.
        # 상시 존재(cov>0.95)는 장면전환을 수십 회 견딘 것 자체가 오버레이 증거.
        inshot = 0
        for iv in ivs:
            for e in (iv[0], iv[1]):
                if 0 < e < n and e not in cuts and dif[e] <= 20:
                    inshot += 1
        if inshot == 0 and len(ivs) < 2 and cov <= 0.95:
            continue
        # Type A 판정: 카드 rect (존재 샘플 2~3곳에서 box 감지)
        # 카드 테두리는 글자보다 훨씬 넓다 — box 감지용 crop은 여백 120px
        px0 = max(0, bx0 - 120); py0 = max(0, by0 - 120)
        pw = min(W, bx1 + 120) - px0; ph = min(H, by1 + 120) - py0
        pw = max(48, pw // 16 * 16); ph = max(48, ph // 16 * 16)
        px0 = max(0, min(px0, W - pw)); py0 = max(0, min(py0, H - ph))
        mid_idx = [iv[0] + (iv[1] - iv[0]) // 2 for iv in ivs][:3]
        rect_votes = []
        for i in mid_idx:
            crop = samples[i][py0:py0 + ph, px0:px0 + pw]
            try:
                cl2 = h29.glyph_clusters(crop)
                gm = h29.rasterize(cl2, pw, ph) if cl2 else np.zeros((ph, pw), np.uint8)
                rect, conf_b, _bd = detect_box(crop, gm, conf_accept=0.6)
            except Exception:
                rect, conf_b = None, 0.0
            if rect is not None:
                rect_votes.append(rect)
        pad_f = int(round(0.5 * fps))
        ivals = [[max(0, iv[0] * scan_step - pad_f), iv[1] * scan_step + pad_f]
                 for iv in ivs]
        if len(rect_votes) >= 2 and cov > 0.95 and len(cuts) < 3:
            continue    # 상시 카드인데 장면전환도 거의 없음 — 실물 오인 위험, 정적 몫
        if len(rect_votes) >= 2:
            # Type A — 반투명 카드. 장시간 분산비로 (α, 카드색) 추정:
            # 카드 안 밝기 분산은 배경 분산의 (1-α)² 배 — 175초 표본이면 안정
            # (UAT-02 실측: 채널별 α 0.496/0.509/0.515, C≈흰색 248 — 일관).
            # 카드는 장면마다 위치가 바뀔 수 있다(UAT-02 실측) — vote마다
            # "그 시점 주변 ±10초 × 그 시점의 rect"로 나눠 추정하고 합산
            vote_est = []
            for vi, v in enumerate(rect_votes):
                vrx0 = v[0] + px0; vry0 = v[1] + py0
                vrx1 = v[2] + px0; vry1 = v[3] + py0
                mid = mid_idx[min(vi, len(mid_idx) - 1)]
                sel_v = [i for i in range(max(0, mid - 25), min(n, mid + 25)) if pres[i]]
                b = _ring_variance_blend(samples, sel_v, vrx0, vry0, vrx1, vry1, W, H,
                                         min_sel=8)
                if b is not None:
                    vote_est.append(b)
            blend = None
            if len(vote_est) >= 2:
                ss2 = [b[0] for b in vote_est]
                if max(ss2) - min(ss2) <= 0.15:      # 시점 간 일관성
                    s_f = round(float(np.mean(ss2)), 4)
                    t_f = [round(float(np.mean([b[1][c] for b in vote_est])), 2)
                           for c in range(3)]
                    blend = (s_f, t_f)
            rx0 = min(v[0] for v in rect_votes) + px0; ry0 = min(v[1] for v in rect_votes) + py0
            rx1 = max(v[2] for v in rect_votes) + px0; ry1 = max(v[3] for v in rect_votes) + py0
            cb_a = max(1, (bx1 - bx0) * (by1 - by0))
            host = None
            for r in (prior_regions or []):
                ix = max(0, min(bx1, r["x"] + r["w"]) - max(bx0, r["x"]))
                iy = max(0, min(by1, r["y"] + r["h"]) - max(by0, r["y"]))
                if ix * iy >= 0.85 * cb_a and rx0 >= r["x"] - 8 and ry0 >= r["y"] - 8 \
                        and rx1 <= r["x"] + r["w"] + 8 and ry1 <= r["y"] + r["h"] + 8:
                    host = r; break
            entry = None
            if blend is not None:
                entry = {"rect": [int(rx0), int(ry0), int(rx1), int(ry1)],
                         "s": blend[0], "t": blend[1]}
            if host is not None:
                # 기존 밴드가 crop하는 영역 — 밴드의 box 경로에 추정값만 공급
                # (밴드 자체 추정이 성공하면 그것을 쓰고, 실패/밝은 카드일 때만 사용)
                if entry is not None:
                    host.setdefault("card_blends", []).append(entry)
                    # 카드 rect 안의 정적 영역은 제거하고, 그 시간축 글자 마스크를
                    # host 밴드 마스크에 병합한다 — 별도 paste는 카드-존재 원본
                    # 기반이라 잔상을 덧칠(run98)하고, 그냥 제거하면 일부 구간
                    # 글자가 남는다(run102 t15). 병합하면 밴드의 un-blend 선적용
                    # 프레임 위에서 AI가 글자를 복원해 두 문제가 모두 사라진다.
                    for sr in list(prior_regions or []):
                        if not str(sr.get("kind", "")).startswith("static"):
                            continue
                        if sr.get("static_mask") is None:
                            continue
                        if sr["x"] >= rx0 - 8 and sr["y"] >= ry0 - 8 \
                                and sr["x"] + sr["w"] <= rx1 + 8 \
                                and sr["y"] + sr["h"] <= ry1 + 8:
                            host.setdefault("extra_masks", []).append(
                                {"off": [int(sr["x"]), int(sr["y"]),
                                         int(sr["w"]), int(sr["h"])],
                                 "mask": sr["static_mask"]})
                            try:
                                prior_regions.remove(sr)
                            except ValueError:
                                pass
                continue
            if cov > 0.95 and entry is None:
                continue    # 상시 카드인데 추정도 실패 — 기존 경로 유지 (오탐/오처리 방지)
            ax0 = max(0, rx0 - 32); ay0 = max(0, ry0 - 32)
            aw = min(W, rx1 + 32) - ax0; ah = min(H, ry1 + 32) - ay0
            aw = max(48, aw // 16 * 16); ah = max(48, ah // 16 * 16)
            ax0 = max(0, min(ax0, W - aw)); ay0 = max(0, min(ay0, H - ah))
            if total_px + aw * ah > W * H * 0.12:
                continue
            total_px += aw * ah
            reg_a = {"x": int(ax0), "y": int(ay0), "w": int(aw), "h": int(ah),
                     "kind": "transient_box" + str(len(regs)), "ivals": ivals,
                     "conf": "high" if inshot >= 1 else "medium", "src": "windowed"}
            if entry is not None:
                reg_a["card_blends"] = [entry]
            else:
                reg_a["force_ai"] = True   # 추정 실패 — AI 경로 (un-blend 오추정 방지)
            regs.append(reg_a)
            continue
        if cov > 0.95:
            continue                             # 상시 존재 텍스트 = 전체 정적/코너 몫
        # 전체-영상 정적 감지가 이미 담당하는 클러스터는 발화 억제 —
        # 정적 경로의 정밀 획 마스크가 품질·비용 모두 우위 (UAT-01 고정자막 실측)
        cb_area = max(1, (bx1 - bx0) * (by1 - by0))
        skip_static = False
        for r in (prior_regions or []):
            if r.get("static_mask") is None and "static_pack" not in r:
                continue
            ix = max(0, min(bx1, r["x"] + r["w"]) - max(bx0, r["x"]))
            iy = max(0, min(by1, r["y"] + r["h"]) - max(by0, r["y"]))
            if ix * iy >= 0.85 * cb_area:
                skip_static = True; break
        if skip_static:
            continue
        # Type B 영역 확장: 존재 구간의 글자 상자 union으로 넓힌다 —
        # 넓은 캡션의 좌우 꼬리가 클러스터 밖이면 잔존 (2차 UAT 실측).
        # 2회 반복(연결 확장), 성장 한도 각 방향 +260px.
        ex0, ey0, ex1, ey1 = bx0, by0, bx1, by1
        for _pass in range(2):
            for si in c["hits"]:
                if not pres[si]:
                    continue
                for x0, y0, x1, y1 in items_t[si]:
                    # 가로로만 잇는다(같은 줄의 꼬리 글자) — 세로 확장은 이웃
                    # 자막 줄을 삼켜 영역이 폭주 (UAT-01 실측)
                    if x0 < ex1 + 60 and x1 > ex0 - 60 and y0 < ey1 + 8 and y1 > ey0 - 8:
                        ex0 = min(ex0, x0); ey0 = min(ey0, y0)
                        ex1 = max(ex1, x1); ey1 = max(ey1, y1)
        ex0 = max(bx0 - 260, ex0); ey0 = max(by0 - 40, ey0)
        ex1 = min(bx1 + 260, ex1); ey1 = min(by1 + 40, ey1)
        if (ex0, ey0, ex1, ey1) != (bx0, by0, bx1, by1):
            fx0 = max(0, ex0 - 24); fy0 = max(0, ey0 - 24)
            gw = min(W, ex1 + 24) - fx0; gh = min(H, ey1 + 24) - fy0
            gw = max(48, gw // 16 * 16); gh = max(48, gh // 16 * 16)
            fx0 = max(0, min(fx0, W - gw)); fy0 = max(0, min(fy0, H - gh))
            area_frac = gw * gh / float(W * H)
            if area_frac > 0.10:
                continue                         # 확장 후에도 대면적 금지
        # Type B — 워터마크: "구간별" 시간축 누적 마스크 (전 구간 union은
        # 서로 다른 시점의 글자 자리를 전부 합쳐 AI가 넓게 뭉갬 — run91 실측).
        # 구간마다 그 구간의 글자 상자 union + edge 지속성만 마스크로 만든다.
        iv_masks = []
        for (a0, b0) in ivs:
            m = np.zeros((gh, gw), np.uint8)
            for si in c["hits"]:
                if not (a0 <= si < b0):
                    continue
                for x0, y0, x1, y1 in items_t[si]:
                    if x1 < fx0 or x0 > fx0 + gw or y1 < fy0 or y0 > fy0 + gh:
                        continue
                    mx0 = max(0, x0 - fx0 - 2); my0 = max(0, y0 - fy0 - 2)
                    mx1 = min(gw, x1 - fx0 + 3); my1 = min(gh, y1 - fy0 + 3)
                    if mx1 > mx0 and my1 > my0:
                        m[my0:my1, mx0:mx1] = 255
            sel = list(range(a0, min(b0, a0 + 48)))
            acc = np.zeros((gh, gw), np.float32)
            for i in sel:
                g = cv2.cvtColor(samples[i][fy0:fy0 + gh, fx0:fx0 + gw],
                                 cv2.COLOR_RGB2GRAY).astype(np.int16)
                gx = np.abs(np.diff(g, axis=1)); gx = np.pad(gx, ((0, 0), (0, 1)))
                gy = np.abs(np.diff(g, axis=0)); gy = np.pad(gy, ((0, 1), (0, 0)))
                acc += ((gx + gy) > 18)
            esig = (acc / max(1, len(sel)) > 0.6).astype(np.uint8) * 255
            near = cv2.dilate((m > 0).astype(np.uint8), np.ones((3, 3), np.uint8),
                              iterations=11) > 0      # 글자 union의 ±32px 근방
            esig[~near] = 0                           # 배경 edge 삼킴 방지
            iv_masks.append(cv2.dilate(np.maximum(m, esig),
                                       np.ones((3, 3), np.uint8), iterations=2))
        if not any(float((m > 0).mean()) >= 0.005 for m in iv_masks):
            continue
        if total_px + gw * gh > W * H * 0.12:
            continue
        total_px += gw * gh
        union = iv_masks[0].copy()
        for m in iv_masks[1:]:
            union = np.maximum(union, m)
        regs.append({"x": int(fx0), "y": int(fy0), "w": int(gw), "h": int(gh),
                     "kind": "transient" + str(len(regs)), "static_mask": union,
                     "ival_masks": iv_masks, "ivals": ivals,
                     "conf": "high" if (inshot >= 1 and support >= 0.3) else "medium",
                     "src": "windowed"})
    return regs


def _ring_variance_blend(samples, sel, rx0, ry0, rx1, ry1, W, H, min_sel=20):
    """반투명 카드 (s=1-α, t=α·C) 추정 — 경계 인접 픽셀쌍 회귀.
    카드 안쪽 가장자리(+4px)의 실제 배경은 바로 바깥(+8px) 픽셀과 연속이므로
    obs_in = s·obs_out + t 를 표본 프레임들에서 강건 회귀로 푼다
    (UAT-02 실측: 5회 반복 수렴 s=0.502, α=0.498, C≈254 — 채널 일관).
    검증: 인라이어 40%+, 0.15≤s≤0.9, 카드색 -20..300. 실패 시 None."""
    if len(sel) < min_sel or rx1 - rx0 < 120 or ry1 - ry0 < 50:
        return None
    xa = rx0 + int(0.2 * (rx1 - rx0)); xb = rx0 + int(0.8 * (rx1 - rx0))
    if ry0 - 8 < 0 or ry1 + 8 >= H:
        return None
    X = []; Y = []
    for i in sel[:: max(1, len(sel) // 60)]:
        fr = samples[i].astype(np.float32)
        Y.append(fr[ry0 + 4, xa:xb]); X.append(fr[ry0 - 8, xa:xb])
        Y.append(fr[ry1 - 4, xa:xb]); X.append(fr[ry1 + 8, xa:xb])
    X = np.concatenate(X).reshape(-1, 3); Y = np.concatenate(Y).reshape(-1, 3)
    if len(X) < 400:
        return None
    keep = np.ones(len(X), bool)
    s = 1.0; t = np.zeros(3, np.float32)
    for _it in range(5):
        if keep.sum() < 200:
            return None
        Xa = np.concatenate([X[keep][:, c] for c in range(3)])
        Ya = np.concatenate([Y[keep][:, c] for c in range(3)])
        if float(Xa.std()) < 10.0:
            return None                  # 경계 밖 배경 대비 부족 — 추정 불가
        s, _b = np.polyfit(Xa, Ya, 1)
        t = np.array([np.median(Y[keep][:, c] - s * X[keep][:, c]) for c in range(3)])
        res = np.abs(Y - (s * X + t)).mean(1)
        keep = res < 10
    if float(keep.mean()) < 0.4 or not (0.15 <= s <= 0.9):
        return None
    alpha = 1.0 - float(s)
    C = [float(tc) / max(alpha, 1e-3) for tc in t]
    if not all(-20.0 <= c <= 300.0 for c in C):
        return None
    return round(float(s), 4), [round(float(tc), 2) for tc in t]


# ---------------- 단계: scan (가벼운 계획 — 영역·구간만, 마스크 없음) ----------------
def scan_v32(proj, tmp, scan_step=12, seg_k=10):
    pid = proj["id"]
    sw = SW()
    h29.set_proj(pid, "wm_running", "[v32] 영상을 받아 오는 중…")
    src, work, info, N = fetch_source_fast(proj, tmp, sw)
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
        regions.extend(detect_sub_bands_shm(samples, W, H))
        for side in ("tl", "tr"):
            c = h29.detect_corner_from(samples, W, H, side)
            if c: regions.append(c)
        try:
            regions.extend(detect_static_overlays(samples, W, H))
        except Exception:
            pass
        try:
            regions = _scene_text_veto(
                regions, getattr(detect_sub_bands_shm, "_last_items_t", None),
                samples, W, H)
        except Exception:
            pass
        try:
            regions.extend(detect_windowed_transient_overlays(
                samples, W, H, scan_step, info["fps"], prior_regions=regions,
                items_t=getattr(detect_sub_bands_shm, "_last_items_t", None)))
        except Exception:
            pass
        del samples
        sw.mark("scan")
    # 박스형 자막: 감지 영역이 박스보다 좁으면 세그 단계에서 복원 불가 →
    # scan 샘플에서 박스를 감지해 region을 박스 전체로 확장 (Phase B)
    if mode == "auto" and regions:
        try:
            samples2 = list(h29.stream_frames(work, W, H, sample_every=scan_step * 3))[:6]
            for reg in regions:
                if reg.get("static_mask") is not None:
                    continue
                if str(reg.get("kind", "")).startswith("transient"):
                    continue    # transient 영역은 이미 rect+여유로 확정 — 재확장 금지
                votes = []
                for fr in samples2:
                    crop = fr[reg["y"]:reg["y"] + reg["h"], reg["x"]:reg["x"] + reg["w"]]
                    cl = h29.glyph_clusters(crop)
                    if not cl:
                        continue
                    gmm = h29.rasterize(cl, reg["w"], reg["h"])
                    rect, conf, _bd = detect_box(crop, gmm)
                    if rect is not None:
                        votes.append(rect)
                if len(votes) >= 2:
                    bx0 = min(v[0] for v in votes) + reg["x"]
                    by0 = min(v[1] for v in votes) + reg["y"]
                    bx1 = max(v[2] for v in votes) + reg["x"]
                    by1 = max(v[3] for v in votes) + reg["y"]
                    # rect가 crop 경계에 닿아 있으면 실제 경계는 밖 → 여유를 크게
                    mt = 140 if any(v[1] <= 2 for v in votes) else 8
                    mb = 140 if any(v[3] >= reg["h"] - 2 for v in votes) else 8
                    ml = 140 if any(v[0] <= 2 for v in votes) else 8
                    mr = 140 if any(v[2] >= reg["w"] - 2 for v in votes) else 8
                    nx = max(0, min(reg["x"], bx0 - ml))
                    ny = max(0, min(reg["y"], by0 - mt))
                    nx1 = min(W, max(reg["x"] + reg["w"], bx1 + mr))
                    ny1 = min(H, max(reg["y"] + reg["h"], by1 + mb))
                    nw = h29.floor16(nx1 - nx) if hasattr(h29, "floor16") else (nx1 - nx) // 16 * 16
                    nh = h29.floor16(ny1 - ny) if hasattr(h29, "floor16") else (ny1 - ny) // 16 * 16
                    if nw >= reg["w"] and nh >= reg["h"] and nw > 0 and nh > 0:
                        if nx + nw > W: nx = W - nw
                        if ny + nh > H: ny = H - nh
                        reg.update(x=int(nx), y=int(ny), w=int(nw), h=int(nh))
            sw.mark("box_expand")
        except Exception:
            pass
    if not regions:
        h29.set_proj(pid, "wm_done", {"note": "no_target", "ver": V32_VER,
            "msg": "지울 자막·워터마크를 찾지 못했어요. [직접 지정] 모드로 영역을 그려서 다시 시도해 주세요."})
        return {"note": "no_target"}
    plan_regions = []
    for reg in regions:
        reg2 = {k: v for k, v in reg.items()
                if k not in ("static_mask", "ival_masks", "extra_masks")}
        if reg.get("static_mask") is not None:
            reg2.update(_pack_static(reg["static_mask"]))
        if reg.get("ival_masks"):
            reg2["ival_packs"] = [
                base64.b64encode(np.packbits(m > 20).tobytes()).decode()
                for m in reg["ival_masks"]]
        if reg.get("extra_masks"):
            reg2.pop("extra_masks", None)
            reg2["extra_packs"] = [
                {"off": em["off"],
                 "pack": base64.b64encode(np.packbits(em["mask"] > 20).tobytes()).decode()}
                for em in reg["extra_masks"]]
        plan_regions.append(reg2)
    K = max(1, min(int(seg_k), 16))
    segments = [[k * N // K, (k + 1) * N // K] for k in range(K)]
    plan = {"W": W, "H": H, "fps": info["fps"], "N": N, "audio": info["audio"],
            "mode": mode, "tier": proj.get("wm_tier") or "std", "cfr": cfr,
            "ver": V32_VER, "regions": plan_regions, "segK": K, "segments": segments}
    tmp_upload(pid, "plan.json", json.dumps(plan).encode(), "application/json")
    sw.mark("plan_up")
    return {"phase": "scan_v32", "regions": len(plan_regions), "N": N,
            "segments": segments, "ver": V32_VER, "tms": sw.out(),
            "veto_dbg": getattr(_scene_text_veto, "_last_dbg", None)}


# ---------------- 박스형(예능체·반투명) 자막 오버레이 감지 (Phase B) ----------------
def _row_step_profile(gray, x0, x1):
    band = gray[:, x0:x1].mean(axis=1)
    k = 5
    prof = np.zeros_like(band)
    for y in range(k, len(band) - k):
        prof[y] = abs(band[y:y + k].mean() - band[y - k:y].mean())
    return prof


def detect_box(frame_rgb, glyph_mask, conf_accept=0.6):
    """글자를 둘러싼 사각 오버레이(박스자막) 감지. (rect|None, conf, diag)
    가드: 경계 edge 필수 / 글자 내포 필수 / 면적 상한 / 내부 색이동·대비감소 증거."""
    h, w = frame_rgb.shape[:2]
    if glyph_mask is None or not glyph_mask.any():
        return None, 0.0, {"why": "no_glyph"}
    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    gm = glyph_mask > 0
    gmd = cv2.dilate(gm.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    _ys, _xs = np.nonzero(gm)
    gx0, gx1 = int(_xs.min()), int(_xs.max()) + 1
    gy0, gy1 = int(_ys.min()), int(_ys.max()) + 1
    prof = _row_step_profile(gray, max(0, gx0 - 20), min(w, gx1 + 20))
    thr = max(8.0, float(np.percentile(prof, 90)) * 0.8)
    up = [y for y in range(max(0, gy0 - 220), gy0) if prof[y] > thr]
    dn = [y for y in range(gy1, min(h, gy1 + 220)) if prof[y] > thr]
    # 경계 개방: 탐색 범위가 crop 가장자리에 닿았는데 edge가 없으면
    # 박스가 crop 밖으로 이어진 것 → crop 가장자리를 경계로 간주 (한쪽은 실제 edge 필수)
    top_open = (gy0 - 220) <= 0
    bot_open = (gy1 + 220) >= h
    open_edge = False
    if not up and not dn:
        return None, 0.0, {"why": "no_h_edges"}
    if not up:
        if not top_open:
            return None, 0.0, {"why": "no_h_edges"}
        up = [0]; open_edge = True
    if not dn:
        if not bot_open:
            return None, 0.0, {"why": "no_h_edges"}
        dn = [h - 1]; open_edge = True
    by0, by1 = max(up), min(dn) + 1
    band = gray[by0:by1]
    k = 5
    cb = band.mean(axis=0)
    colp = np.zeros(w)
    for x in range(k, w - k):
        colp[x] = abs(cb[x:x + k].mean() - cb[x - k:x].mean())
    cthr = max(8.0, float(np.percentile(colp, 90)) * 0.8)
    # 좌우는 전 구간 탐색 — edge가 전혀 없으면 박스가 전체폭(crop 밖까지)인 것
    lf = [x for x in range(0, gx0) if colp[x] > cthr]
    rt = [x for x in range(gx1, w) if colp[x] > cthr]
    bx0 = max(lf) if lf else 0
    bx1 = (min(rt) + 1) if rt else w
    bx0 = min(bx0, max(0, gx0 - 4)); bx1 = max(bx1, min(w, gx1 + 4))
    by0 = min(by0, max(0, gy0 - 4)); by1 = max(by1, min(h, gy1 + 4))
    # 경계 스냅: threshold 교차점이 아니라 step 피크 행/열로 정밀 정렬 (±6)
    # (un-blend 띠 표본이 2~3px 오차로도 오염되므로 필수 — 로컬 검증)
    if up and by0 > 0:
        lo, hi = max(1, by0 - 6), min(h - 1, by0 + 7)
        if hi > lo: by0 = lo + int(np.argmax(prof[lo:hi]))
    if dn and by1 < h:
        lo, hi = max(1, by1 - 7), min(h - 1, by1 + 6)
        if hi > lo: by1 = lo + int(np.argmax(prof[lo:hi])) + 1
    if lf and bx0 > 0:
        lo, hi = max(1, bx0 - 6), min(w - 1, bx0 + 7)
        if hi > lo: bx0 = lo + int(np.argmax(colp[lo:hi]))
    if rt and bx1 < w:
        lo, hi = max(1, bx1 - 7), min(w - 1, bx1 + 6)
        if hi > lo: bx1 = lo + int(np.argmax(colp[lo:hi])) + 1
    rect = (int(bx0), int(by0), int(bx1), int(by1))
    rect_area = (bx1 - bx0) * (by1 - by0)
    area_ratio = rect_area / float(w * h)
    glyph_area = int(gm.sum())
    inbox = float(gm[by0:by1, bx0:bx1].sum()) / max(1, glyph_area)
    if (bx1 - bx0) < 0.9 * w:
        cols = list(range(0, max(0, bx0 - 8))) + list(range(min(w, bx1 + 8), w))
        outside = gray[by0:by1][:, cols] if cols else np.zeros((0,))
    else:
        pad = max(8, min(40, by0, h - by1))
        outside = np.concatenate([gray[max(0, by0 - pad):by0],
                                  gray[by1:min(h, by1 + pad)]])
    inner_mask = ~gmd[by0:by1, bx0:bx1]
    inner_vals = gray[by0:by1, bx0:bx1][inner_mask]
    if inner_vals.size < 100 or outside.size < 100:
        return None, 0.0, {"why": "no_stats"}
    shift = abs(float(inner_vals.mean()) - float(outside.mean()))
    damp = float(inner_vals.std()) < 0.8 * float(outside.std()) + 4
    conf = 0.30
    if inbox >= 0.90: conf += 0.20
    if area_ratio <= 0.80: conf += 0.15
    if rect_area <= 25 * max(1, glyph_area): conf += 0.10
    if shift >= 8 or damp: conf += 0.25
    else: conf = min(conf, 0.40)
    if open_edge: conf -= 0.05
    diag = {"rect": rect, "conf": round(conf, 2), "shift": round(shift, 1),
            "inbox": round(inbox, 3), "area_ratio": round(area_ratio, 3),
            "open_edge": open_edge}
    if conf < conf_accept or inbox < 0.90 or area_ratio > 0.85:
        return None, conf, dict(diag, why="low_conf")
    diag["std_in"] = round(float(inner_vals.std()), 1)
    return rect, conf, diag


def _estimate_blend_group(frames, rects, gkeys, raw, hh, ww):
    """그룹(시간축) 집계로 (slope=1-α, intercept(3,)=α·C) 추정 — 로컬 검증 최종안.
    slope: 박스 내부 deep 영역 vs 외부 ring의 IQR(P90-P10) 비율 (시간 집계로
           콘텐츠 편향 평균화). intercept: slope 고정 후 경계 인접 띠(글자 제외
           masked-blur, gap6) 잔차의 중앙값. 표본 부족 시 None(→ AI 폴백)."""
    INs = [[] for _ in range(3)]
    OUTs = [[] for _ in range(3)]
    PAIRS = []
    GAP, SW = 6, 4
    for i in gkeys:
        x0, y0, x1, y1 = rects[i]
        if y1 - y0 < 24 or x1 - x0 < 24:
            continue
        gmd = cv2.dilate((raw[i] > 0).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        f = frames[i].astype(np.float32)
        inner = f[y0 + 8:y1 - 8, x0 + 6:x1 - 6]
        im = ~gmd[y0 + 8:y1 - 8, x0 + 6:x1 - 6]
        ring = []
        if y0 >= 14: ring.append(f[max(0, y0 - 40):y0 - 8].reshape(-1, 3))
        if y1 <= hh - 14: ring.append(f[y1 + 8:min(hh, y1 + 40)].reshape(-1, 3))
        if x0 >= 14: ring.append(f[y0:y1, max(0, x0 - 40):x0 - 8].reshape(-1, 3))
        if x1 <= ww - 14: ring.append(f[y0:y1, x1 + 8:min(ww, x1 + 40)].reshape(-1, 3))
        if ring and im.sum() > 400:
            rg = np.concatenate(ring, axis=0)
            for ch in range(3):
                INs[ch].append(inner[..., ch][im])
                OUTs[ch].append(rg[:, ch])
        mk = (~gmd).astype(np.float32)
        fxm = cv2.blur(f * mk[..., None], (41, 1)) / np.maximum(
            cv2.blur(mk, (41, 1)), 1e-3)[..., None]
        dnx = cv2.blur(mk, (41, 1))
        fym = cv2.blur(f * mk[..., None], (1, 41)) / np.maximum(
            cv2.blur(mk, (1, 41)), 1e-3)[..., None]
        dny = cv2.blur(mk, (1, 41))
        def _h_edge(e, sgn):
            if sgn > 0:
                Bs, Cs = fxm[e - GAP - SW:e - GAP], fxm[e + GAP:e + GAP + SW]
                dB, dC = dnx[e - GAP - SW:e - GAP], dnx[e + GAP:e + GAP + SW]
            else:
                Bs, Cs = fxm[e + GAP:e + GAP + SW], fxm[e - GAP - SW:e - GAP]
                dB, dC = dnx[e + GAP:e + GAP + SW], dnx[e - GAP - SW:e - GAP]
            if Bs.shape[0] < SW or Cs.shape[0] < SW:
                return
            for xx in range(x0 + 8, x1 - 8, 2):
                if dB[:, xx].min() < 0.7 or dC[:, xx].min() < 0.7:
                    continue
                PAIRS.append((Cs[:, xx].mean(axis=0), Bs[:, xx].mean(axis=0)))
        def _v_edge(e, sgn):
            if sgn > 0:
                Bs, Cs = fym[:, e - GAP - SW:e - GAP], fym[:, e + GAP:e + GAP + SW]
                dB, dC = dny[:, e - GAP - SW:e - GAP], dny[:, e + GAP:e + GAP + SW]
            else:
                Bs, Cs = fym[:, e + GAP:e + GAP + SW], fym[:, e - GAP - SW:e - GAP]
                dB, dC = dny[:, e + GAP:e + GAP + SW], dny[:, e - GAP - SW:e - GAP]
            if Bs.shape[1] < SW or Cs.shape[1] < SW:
                return
            for yy in range(y0 + 8, y1 - 8, 2):
                if dB[yy].min() < 0.7 or dC[yy].min() < 0.7:
                    continue
                PAIRS.append((Cs[yy].mean(axis=0), Bs[yy].mean(axis=0)))
        if y0 >= GAP + SW: _h_edge(y0, +1)
        if y1 <= hh - GAP - SW: _h_edge(y1 - 1, -1)
        if x0 >= GAP + SW: _v_edge(x0, +1)
        if x1 <= ww - GAP - SW: _v_edge(x1 - 1, -1)
    if not INs[0] or len(PAIRS) < 60:
        return None
    ss = []
    for ch in range(3):
        i_all = np.concatenate(INs[ch]); o_all = np.concatenate(OUTs[ch])
        if i_all.size < 2000 or o_all.size < 2000:
            return None
        iq_i = np.percentile(i_all, 90) - np.percentile(i_all, 10)
        iq_o = np.percentile(o_all, 90) - np.percentile(o_all, 10)
        ss.append(iq_i / max(iq_o, 1.0))
    s = float(np.clip(np.median(ss), 0.05, 1.2))
    C = np.array([p[0] for p in PAIRS], np.float32)
    B = np.array([p[1] for p in PAIRS], np.float32)
    t_vec = np.array([float(np.median(C[:, ch] - s * B[:, ch])) for ch in range(3)],
                     np.float32)
    return s, t_vec


def stable_boxes(rects_by_key, min_support=2):
    """키프레임별 박스 rect의 시간축 안정성: IoU>0.5 그룹(합집합 rect), 지지 키 반환."""
    def iou(a, b):
        ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
        ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
        if ix1 <= ix0 or iy1 <= iy0: return 0.0
        inter = (ix1 - ix0) * (iy1 - iy0)
        ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
        return inter / ua
    groups = []
    for kk, r in rects_by_key.items():
        for g in groups:
            if iou(g[0], r) > 0.5:
                g[1].append(kk)
                g[0] = (min(g[0][0], r[0]), min(g[0][1], r[1]),
                        max(g[0][2], r[2]), max(g[0][3], r[3]))
                break
        else:
            groups.append([list(r) and r, [kk]])
    total = len(rects_by_key)
    need = 1 if total < min_support else min_support
    return [(tuple(g[0]), g[1]) for g in groups if len(g[1]) >= need]


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
        if reg.get("ivals"):
            # transient: 등장 구간 밖은 zero mask → 조각 계획·AI 비용이 구간 안으로 제한.
            # 구간별 마스크(ival_packs)가 있으면 각 구간은 자기 구간의 글자 자리만 가린다
            # (전 구간 union은 넓은 뭉갬 유발 — run91 실측).
            hh2, ww2 = reg["static_shape"]
            iv_m = None
            if reg.get("ival_packs"):
                iv_m = []
                for p in reg["ival_packs"]:
                    bits = np.frombuffer(base64.b64decode(p), np.uint8)
                    iv_m.append((np.unpackbits(bits, count=hh2 * ww2)
                                 .reshape(hh2, ww2) * 255).astype(np.uint8))
            zeros = np.zeros_like(m)
            out = []; cnt = 0
            for li in range(n):
                gi = li + e0_global
                pick = None
                for k2, (a, b) in enumerate(reg["ivals"]):
                    if int(a) <= gi < int(b):
                        pick = iv_m[k2] if iv_m is not None else m
                        break
                if pick is not None and pick.any():
                    out.append(pick); cnt += 1
                else:
                    out.append(zeros)
            return out, cnt
        return [m] * n, n
    # 자막/라벨 밴드: 키프레임 정밀 감지 (공유메모리 풀 — v31이 h29._par_sweep에 이식)
    ks_i = max(1, int(key_step))
    keys = list(range(0, n, ks_i))
    per = [None] * n
    with_labels = not kind.startswith("subtitle")
    if with_labels:
        res = h29._par_sweep("cl", frames_local, n, ks_i)
        if res is None:
            res = [h29.glyph_clusters(frames_local[i]) for i in keys]
        for k, i in enumerate(keys):
            per[i] = res[k]
    else:
        # v29 subtitle 경로 이식: 색상판 프루닝 → cl_ss(싱글 포함) → 줄 기반 단글자 승인
        active = set()
        r2 = h29._par_sweep("prune", frames_local, n, 15)
        if r2 is None:
            r2 = [h29.glyph_clusters(frames_local[i], with_singles=True)
                  for i in range(0, n, 15)]
        for cl0, ss0 in r2:
            for c in cl0:
                for i2 in c.get("items") or []:
                    if "plane" in i2: active.add(i2["plane"])
            for s2 in ss0:
                for i2 in s2["items"]:
                    if "plane" in i2: active.add(i2["plane"])
        sing = [None] * n
        if active:
            res = h29._par_sweep("cl_ss", frames_local, n, ks_i, planes=active)
            if res is None:
                res = [h29.glyph_clusters(frames_local[i], with_singles=True, only_planes=active)
                       for i in keys]
            for k, i in enumerate(keys):
                per[i], sing[i] = res[k]
        else:
            for i in keys:
                per[i] = []; sing[i] = []
        # 줄(line) 통계 — 키프레임에서 관측된 여러 글자 자막 줄 (v29와 동일 규칙, 관측 수 기준은
        # 키프레임 수에 비례해 축소: 전 프레임 8회 ≈ 키프레임 max(2, 8//ks) 회)
        lines = []
        for fi in keys:
            for c in per[fi] or []:
                its = c.get("items") or []
                if len(its) < 2 or "h" not in its[0]: continue
                yc = (c["y0"] + c["y1"]) / 2
                hmed = sorted(i2["h"] for i2 in its)[len(its) // 2]
                put = None
                for L in lines:
                    if abs(L["yc"] - yc) < 25: put = L; break
                if put:
                    put["n"] += 1; put["ys"].append(yc); put["hs"].append(hmed)
                    put["x0"] = min(put["x0"], c["x0"]); put["x1"] = max(put["x1"], c["x1"])
                    put["yc"] = sum(put["ys"]) / len(put["ys"])
                    put["fr"].append(fi)
                else:
                    lines.append({"yc": yc, "n": 1, "ys": [yc], "hs": [hmed],
                                  "x0": c["x0"], "x1": c["x1"], "fr": [fi]})
        need_n = max(2, 8 // ks_i)
        lines = [L for L in lines if L["n"] >= need_n]
        for L in lines:
            L["hmed"] = sorted(L["hs"])[len(L["hs"]) // 2]
            act = np.zeros(n + 1, np.int32)
            for fi in L["fr"]: act[fi] = 1
            L["cum"] = np.cumsum(act)
        def _near(L, i, win=90):
            a = max(0, i - win); b = min(n - 1, i + win)
            return (L["cum"][b + 1] - L["cum"][a]) > 0
        if lines:
            for i in keys:
                for s2 in sing[i] or []:
                    syc = (s2["y0"] + s2["y1"]) / 2
                    sh = max(i2["h"] for i2 in s2["items"])
                    for L in lines:
                        if abs(L["yc"] - syc) < 30 and L["x0"] - 30 <= s2["x0"]                            and s2["x1"] <= L["x1"] + 30                            and 0.6 * L["hmed"] <= sh <= 1.45 * L["hmed"] and _near(L, i):
                            per[i].append(s2); break
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
    # 박스형 자막 (Phase B v9):
    #  - 반투명 박스(α≤0.75): 그룹 시간축 집계로 (α, 박스색) 정밀 추정 후 역블렌딩.
    #    (전체폭 밴드 AI 복원은 뭉개짐 — run5 증거. 코덱 정보소실로 PSNR 상한
    #     ~23-25는 물리 한계 — 게이트는 이 상한 기준으로 보정됨)
    #  - 불투명 박스(α>0.75) 또는 추정 실패: AI 복원 (+12px 테두리, scale 1.0)
    #  - 오탐 차단: 시간축 지지도 필터 — 지지 키 < max(3, 40%·masked) 그룹 기각
    #  - 이동 박스: 프레임별 rect를 인접 키 사이 선형 보간
    box_stats = {"box_keys": 0, "box_conf_max": 0.0, "box_low_conf": 0,
                 "box_unblend": 0, "box_ai": 0, "box_rej": 0}
    box_fixes = {}   # local frame idx -> [(rect, gain(3,), bias(3,))]
    if not kind.startswith("manual") and masked:
        rects_by_key = {}
        for i in keys:
            if raw[i] is None:
                continue
            rect, conf, _bd = detect_box(frames_local[i], raw[i])
            box_stats["box_conf_max"] = max(box_stats["box_conf_max"], round(conf, 2))
            if rect is not None:
                rects_by_key[i] = rect
            elif 0.4 <= conf < 0.6:
                box_stats["box_low_conf"] += 1     # review-required 신호
        need = max(3, int(round(0.4 * masked)))
        ring2 = 6 + (max(1, int(key_step)) - 1 + 1) // 2
        for grect, gkeys in stable_boxes(rects_by_key):
            if len(gkeys) < need:
                # 이동 카드: 위치가 바뀐 뒤 키 수가 적어 기각되던 그룹도
                # scan이 추정해 둔 카드(크기 매칭)면 un-blend 채택 (t45 실측)
                rescue = False
                for cb in (reg.get("card_blends") or []):
                    cw = cb["rect"][2] - cb["rect"][0]; ch2 = cb["rect"][3] - cb["rect"][1]
                    gw2 = grect[2] - grect[0]; gh2 = grect[3] - grect[1]
                    if 0.5 * cw <= gw2 <= 2.0 * cw and 0.5 * ch2 <= gh2 <= 2.0 * ch2 \
                            and len(gkeys) >= 2:
                        rescue = True; break
                if not rescue:
                    box_stats["box_rej"] += 1
                    continue
            box_stats["box_keys"] += len(gkeys)
            est = None if reg.get("force_ai") else \
                _estimate_blend_group(frames_local, rects_by_key, gkeys, raw, hh, ww)
            # 밝은 반투명 카드는 그룹 un-blend 추정이 불안정(어두운 잔상 실측, run91)
            # → 복원 색이 밝으면(t/(1-s)>150) 그룹 추정을 버린다
            if est is not None and est[0] >= 0.25:
                s_chk, t_chk = est
                bright = float(np.mean(t_chk)) / max(0.05, 1.0 - float(s_chk))
                if bright > 150.0:
                    est = None
            # 2차: scan이 장시간 분산비로 추정한 카드 (α, 색) — 그룹 추정 실패 시만
            if (est is None or est[0] < 0.25) and reg.get("card_blends"):
                gx0, gy0, gx1, gy1 = grect
                for cb in reg["card_blends"]:
                    # 카드가 장면마다 이동 — 위치 대신 크기 유사성으로 매칭
                    cw = cb["rect"][2] - cb["rect"][0]; ch = cb["rect"][3] - cb["rect"][1]
                    gw2 = gx1 - gx0; gh2 = gy1 - gy0
                    size_ok = (0.5 * cw <= gw2 <= 2.0 * cw and 0.5 * ch <= gh2 <= 2.0 * ch)
                    if size_ok and 0.2 <= float(cb["s"]) <= 0.85:
                        est = (float(cb["s"]), np.array(cb["t"], np.float32))
                        box_stats["box_scan_blend"] = box_stats.get("box_scan_blend", 0) + 1
                        # 카드 테두리는 내부보다 불투명해 un-blend 후 halo가 남는다
                        # → 테두리 링(±10px)을 AI 마스크에 추가 (un-blend된 배경 위 복원)
                        for i2 in gkeys:
                            r2b = rects_by_key[i2]
                            ox0 = max(0, r2b[0] - 10); oy0 = max(0, r2b[1] - 10)
                            ox1 = min(ww, r2b[2] + 10); oy1 = min(hh, r2b[3] + 10)
                            ring_m = np.zeros((hh, ww), np.uint8)
                            ring_m[oy0:oy1, ox0:ox1] = 255
                            ix0 = min(ww, r2b[0] + 10); iy0 = min(hh, r2b[1] + 10)
                            ix1 = max(0, r2b[2] - 10); iy1 = max(0, r2b[3] - 10)
                            if ix1 > ix0 and iy1 > iy0:
                                ring_m[iy0:iy1, ix0:ix1] = 0
                            raw[i2] = np.maximum(raw[i2], ring_m)
                        break
            if est is None or est[0] < 0.25:
                # 불투명(또는 추정 실패): AI 복원 경로
                box_stats["box_ai"] += 1
                x0, y0, x1, y1 = grect
                x0 = max(0, x0 - 12); y0 = max(0, y0 - 12)
                x1 = min(ww, x1 + 12); y1 = min(hh, y1 + 12)
                for i in gkeys:
                    raw[i][y0:y1, x0:x1] = 255
                continue
            # 반투명: bg = (obs - t)/s → gain=1/s, bias=-t/s
            box_stats["box_unblend"] += 1
            s_med, t_med = est
            gain = np.full(3, 1.0 / s_med, np.float32)
            bias = (-t_med / s_med).astype(np.float32)
            gk = sorted(gkeys)
            for i in range(n):
                near = min(gk, key=lambda k: abs(k - i))
                if abs(near - i) > ring2:
                    continue
                prev = max((k for k in gk if k <= i), default=near)
                nxt = min((k for k in gk if k >= i), default=near)
                if prev == nxt:
                    r_i = rects_by_key[prev]
                else:
                    wgt = (i - prev) / float(nxt - prev)
                    ra, rb = rects_by_key[prev], rects_by_key[nxt]
                    r_i = tuple(int(round(a * (1 - wgt) + b * wgt))
                                for a, b in zip(ra, rb))
                box_fixes.setdefault(i, []).append(
                    (tuple(int(v) for v in r_i), gain, bias))
        # un-blend를 AI 입력에 선적용 (AI는 복원된 배경 위 글자만 지움)
        for i, fixes in box_fixes.items():
            fr = frames_local[i]
            if not fr.flags.writeable:
                fr = fr.copy()
                frames_local[i] = fr
            for (x0, y0, x1, y1), gain, bias in fixes:
                sub = fr[y0:y1, x0:x1].astype(np.float32) * gain + bias
                fr[y0:y1, x0:x1] = np.clip(sub, 0, 255).astype(np.uint8)
    _seg_masks_for_region._last_box_stats = box_stats
    _seg_masks_for_region._last_box_fixes = box_fixes
    # ±(6 + ceil((key_step-1)/2)) union — 키 간격의 절반만 넓혀 커버리지 보존 + 과도한 번짐 방지
    ks = max(1, int(key_step))
    ring = 6 + (ks - 1 + 1) // 2
    zeros = np.zeros((hh, ww), np.uint8)
    # 카드 안 정적 글자 마스크 병합 — 시간축 커버를 밴드 마스크에 흡수
    # (un-blend 선적용된 frames_local 위에서 AI가 글자를 복원)
    extra = None
    for ep in (reg.get("extra_packs") or []):
        ex, ey, ew, eh = ep["off"]
        bits = np.frombuffer(base64.b64decode(ep["pack"]), np.uint8)
        em = (np.unpackbits(bits, count=eh * ew).reshape(eh, ew) * 255).astype(np.uint8)
        lx = ex - reg["x"]; ly = ey - reg["y"]
        sx0 = max(0, lx); sy0 = max(0, ly)
        sx1 = min(ww, lx + ew); sy1 = min(hh, ly + eh)
        if sx1 <= sx0 or sy1 <= sy0:
            continue
        if extra is None:
            extra = np.zeros((hh, ww), np.uint8)
        extra[sy0:sy1, sx0:sx1] = np.maximum(
            extra[sy0:sy1, sx0:sx1],
            em[sy0 - ly:sy1 - ly, sx0 - lx:sx1 - lx])
    out = []
    for i in range(n):
        u = None
        for j in range(max(0, i - ring), min(n - 1, i + ring) + 1):
            if raw[j] is not None:
                u = raw[j].copy() if u is None else (u | raw[j])
        if extra is not None:
            u = extra.copy() if u is None else (u | extra)
        out.append(u if u is not None else zeros)
    if extra is not None:
        masked = max(masked, n)
    return out, masked


# ---------------- 단계: segment (GPU) — 로컬 마스크 + AI 복원 + 합성 + 인코딩 ----------------
def segment_v32(proj, tmp, part, key_step=KEY_STEP_DEF):
    t_enter = time.time()
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
    box_fix_by_region = {}  # ri -> {local_i: [(rect, gain, bias)]} (반투명 un-blend v2)
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
        bs = getattr(_seg_masks_for_region, "_last_box_stats", None)
        if bs:
            counters["box_keys"] = counters.get("box_keys", 0) + bs["box_keys"]
            counters["box_low_conf"] = counters.get("box_low_conf", 0) + bs["box_low_conf"]
            counters["box_conf_max"] = max(counters.get("box_conf_max", 0.0),
                                           bs["box_conf_max"])
            counters["box_unblend"] = counters.get("box_unblend", 0) + bs.get("box_unblend", 0)
            counters["box_ai"] = counters.get("box_ai", 0) + bs.get("box_ai", 0)
            counters["box_rej"] = counters.get("box_rej", 0) + bs.get("box_rej", 0)
        bf = getattr(_seg_masks_for_region, "_last_box_fixes", None) or {}
        if bf:
            box_fix_by_region[ri] = bf
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
        if bs and bs.get("box_ai"):
            t2["scale"] = 1.0     # 불투명 박스 AI 복원은 원해상도로 (뭉개짐 방지)
        if str(reg.get("kind", "")).startswith("transient"):
            t2["scale"] = 1.0     # transient 마스크 복원도 원해상도 (뭉갬 방지, 구간 제한이라 저비용)
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
                            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", outp, "-y"], stdin=subprocess.PIPE)
    i = F0
    for fr in v31.stream_frames_range(work, W, H, F0, F1, fps):
        frame = fr.copy()
        # 반투명 박스 un-blend v2 (AI 결과 덮기 전에 배경 역블렌딩)
        for ri, fixes in box_fix_by_region.items():
            fl = fixes.get(i - E0)
            if not fl:
                continue
            reg = plan["regions"][ri]
            for (x0, y0, x1, y1), gain, bias in fl:
                gy0, gy1 = reg["y"] + y0, reg["y"] + y1
                gx0, gx1 = reg["x"] + x0, reg["x"] + x1
                # rect가 crop 경계에 닿아 있으면 박스가 crop 밖으로 이어진 것
                # → 전체 프레임 좌표로 프레임 끝까지 연장 (floor16 crop의 잔여 스트립)
                open_l = x0 <= 2 and reg["x"] > 0
                open_r = x1 >= reg["w"] - 2 and reg["x"] + reg["w"] < W
                open_t = y0 <= 2 and reg["y"] > 0
                open_b = y1 >= reg["h"] - 2 and reg["y"] + reg["h"] < H
                # 연장은 floor16 잔여 스트립 한도(16px)까지만 — 개방 경계 오탐이
                # 프레임 전체를 오염시키는 회귀 방지 (run8 증거)
                if open_l: gx0 = max(0, reg["x"] - 16)
                if open_r: gx1 = min(W, reg["x"] + reg["w"] + 16)
                if open_t: gy0 = max(0, reg["y"] - 16)
                if open_b: gy1 = min(H, reg["y"] + reg["h"] + 16)
                sub = frame[gy0:gy1, gx0:gx1].astype(np.float32) * gain + bias
                fixed = np.clip(sub, 0, 255).astype(np.uint8)
                fh, fw = fixed.shape[:2]
                if fh > 20 and fw > 20:
                    a = np.ones((fh, fw), np.float32)
                    e = 8
                    ramp = np.linspace(0, 1, e, dtype=np.float32)
                    # 프레임 경계로 연장된 쪽은 feather 생략 (경계엔 이음새 없음)
                    if not (open_t or gy0 == 0): a[:e] *= ramp[:, None]
                    if not (open_b or gy1 == H): a[-e:] *= ramp[::-1][:, None]
                    if not (open_l or gx0 == 0): a[:, :e] *= ramp[None, :]
                    if not (open_r or gx1 == W): a[:, -e:] *= ramp[::-1][None, :]
                    orig = frame[gy0:gy1, gx0:gx1].astype(np.float32)
                    frame[gy0:gy1, gx0:gx1] = np.clip(
                        orig * (1 - a[..., None]) + fixed * a[..., None], 0, 255
                    ).astype(np.uint8)
                else:
                    frame[gy0:gy1, gx0:gx1] = fixed
        for ri, rest in seg_rest.items():
            if i not in rest: continue
            reg = plan["regions"][ri]
            if "f0" in reg and not (int(reg["f0"]) <= i < int(reg["f1"])): continue
            if reg.get("ivals") and not any(int(a) <= i < int(b) for a, b in reg["ivals"]): continue
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
            "counters": counters, "tms": sw.out(),
            "container_id": os.environ.get("MODAL_TASK_ID") or os.environ.get("HOSTNAME", "?"),
            "t_enter": round(t_enter, 3), "t_done": round(time.time(), 3)}


# ---------------- 계층형 출력 검증 (Phase D — fault-injection 12종 검증 완료) ----------------
import re as _re
_BAD_DECODE = _re.compile(r"corrupt|invalid|missing picture|concealing|error while decoding|"
                          r"decode_slice_header error|no frame", _re.I)


def _decode_span_check(path, start_s, n_frames):
    r = subprocess.run(["ffmpeg", "-v", "warning", "-err_detect", "explode",
                        "-ss", f"{max(0.0, start_s):.3f}", "-i", path,
                        "-frames:v", str(n_frames), "-f", "null", "-"],
                       capture_output=True, timeout=300)
    bad = [l for l in r.stderr.decode().splitlines() if _BAD_DECODE.search(l)]
    return r.returncode == 0 and not bad, (bad[0][:150] if bad else "")


def validate_output_layer1(path, N, fps, W, H, audio, boundaries):
    """동기 검증 (실측 P50 ~5초): 컨테이너/스트림/해상도/FPS/duration/패킷수/
    오디오길이/시작·중간·끝·세그경계 샘플 디코드. 미탐 잔여(임의 위치 무음 손상)는
    Layer2 전체 디코드(deep audit)가 커버."""
    issues = []
    info = h29.probe_info(path)
    if (info["W"], info["H"]) != (W, H):
        issues.append(f"resolution {info['W']}x{info['H']}")
    if abs(info["fps"] - fps) > 0.02:
        issues.append(f"fps {info['fps']}")
    dur_exp = N / float(fps)
    if abs(info["dur"] - dur_exp) > 0.5:
        issues.append(f"duration {info['dur']:.2f}!={dur_exp:.2f}")
    if audio and not info["audio"]:
        issues.append("no_audio_stream")
    n = frame_count_fast(path)
    if n != N:
        issues.append(f"packet_count {n}!={N}")
    if audio and info["audio"]:
        out = h29.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                       "-show_entries", "stream=duration", "-of", "csv=p=0", path]).stdout
        try:
            adur = float(out.decode().strip().split("\n")[0] or 0)
            if adur and abs(adur - info["dur"]) > 0.5:
                issues.append(f"audio_duration {adur:.2f}")
        except ValueError:
            pass
    spans = [(0.0, 10), (dur_exp / 2, 10), (max(0.0, dur_exp - 12 / fps - 0.5), 12)]
    spans += [(max(0.0, (b - 1) / fps), 3) for b in boundaries]
    for st, nf in spans:
        ok, err = _decode_span_check(path, st, nf)
        if not ok:
            issues.append(f"decode_fail@{st:.1f}s {err}")
            break
    return issues


def deep_audit_full_decode(path, dur_s, jobs=8):
    """Layer2: 전체 프레임 병렬 디코드 — 무음 손상 전수 검출 (fault 12/12 검증).
    staging/C0에서는 동기 실행, 운영에서는 표본/비동기."""
    from concurrent.futures import ThreadPoolExecutor
    span = dur_s / jobs
    def _one(i):
        ss = max(0.0, i * span - (1.0 if i else 0))
        r = subprocess.run(["ffmpeg", "-v", "warning", "-err_detect", "explode",
                            "-ss", f"{ss:.2f}", "-t", f"{span + 1.0:.2f}",
                            "-i", path, "-f", "null", "-"],
                           capture_output=True, timeout=600)
        return [l for l in r.stderr.decode().splitlines() if _BAD_DECODE.search(l)]
    errs = []
    with ThreadPoolExecutor(jobs) as ex:
        for bad in ex.map(_one, range(jobs)):
            errs.extend(bad)
    return errs[:5]


# ---------------- 단계: finish (CPU) ----------------
def _seg_exists(pid, name):
    """세그 도착 확인 — 일시적 네트워크 오류는 '아직 없음'으로 간주 (다음 폴링에 재시도).
    (A3 실험에서 ReadTimeout 1회가 finish 전체를 죽이는 문제 발견 → 방어)"""
    import requests as _rq
    try:
        r = _rq.get(f"{h29.SB_URL}/storage/v1/object/videos-clips/{PFX}/{pid}/{name}",
                    headers=h29.sb_headers({"Range": "bytes=0-0"}), timeout=20)
        return r.status_code in (200, 206)
    except Exception:
        return False


def finish_v32(proj, tmp, t0, parts, tms_in=None, stream=False, wait_s=1500,
               deep_audit=False):
    """stream=True: 세그먼트가 저장소에 '도착하는 대로' 내려받고 검증까지 겹쳐 수행
    (마지막 세그 완료 시점에는 concat+mux+업로드만 남음 — Phase 1 스트리밍 마무리)."""
    pid = proj["id"]
    sw = SW()
    plan = json.loads(tmp_download(pid, "plan.json").decode())
    N, fps = plan["N"], plan["fps"]
    h29.set_proj(pid, "wm_running", "[v32] 마무리 중…")
    from concurrent.futures import ThreadPoolExecutor
    seg_paths = [os.path.join(tmp, f"seg_{k}.mp4") for k in range(parts)]
    counts = [None] * parts
    def _dl_one(k):
        with open(seg_paths[k], "wb") as f:
            f.write(tmp_download(pid, f"seg_{k}.mp4"))
        counts[k] = frame_count_fast(seg_paths[k])
    if stream:
        pending = set(range(parts))
        deadline = time.time() + wait_s
        t_all_seen = None
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {}
            while (pending or futs) and time.time() < deadline:
                for k in list(pending):
                    if _seg_exists(pid, f"seg_{k}.mp4"):
                        futs[k] = ex.submit(_dl_one, k)
                        pending.discard(k)
                if not pending and t_all_seen is None:
                    t_all_seen = time.time()
                for k, fu in list(futs.items()):
                    if fu.done():
                        fu.result()
                        del futs[k]
                if pending or futs:
                    time.sleep(2)
        if pending:
            raise RuntimeError(f"[v32] 세그 대기 시간 초과: 미도착 {sorted(pending)}")
        if t_all_seen is not None:
            sw.t["dl_tail"] = round(time.time() - t_all_seen, 1)
        sw.mark("dl_stream")
    else:
        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(_dl_one, range(parts)))
        sw.mark("dl")
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
    bounds = [seg[0] for seg in plan["segments"][1:]]
    v_issues = validate_output_layer1(outp, N, fps, plan["W"], plan["H"],
                                      bool(plan.get("audio")), bounds)
    sw.mark("validate")
    if v_issues:
        raise RuntimeError(f"[v32] 출력 검증 실패: {'; '.join(v_issues[:4])}")
    if deep_audit or os.environ.get("WM_DEEP_AUDIT") == "1":
        da = deep_audit_full_decode(outp, N / float(fps))
        sw.mark("deep_audit")
        if da:
            raise RuntimeError(f"[v32] deep audit 실패: {da[0]}")
    fin_n = N
    dest = f"{proj['user_id']}/wm_v32_{pid}.mp4"
    out_mb = round(os.path.getsize(outp) / 1e6, 1)
    url_out, up_mode = upload_clip_fast(dest, outp, sw)
    sec = round(time.time() - t0)
    tms = dict(tms_in or {})
    tms["finish"] = sw.out()
    detail = {"url": url_out, "mode": plan.get("mode"), "tier": plan.get("tier"),
              "regions": [r["kind"] for r in plan["regions"]], "sec": sec,
              "gpu": BACKEND_NAME, "ver": V32_VER, "segK": plan.get("segK"),
              "up_mode": up_mode, "tms": tms}
    h29.set_proj(pid, "wm_done", detail)
    names = ["plan.json"] + [f"seg_{k}.mp4" for k in range(parts)] \
        + (["work.mp4"] if plan.get("cfr") else [])
    tmp_delete(pid, names)
    print("[v32] 완료", pid, json.dumps({**detail, "url": "(생략)"}, ensure_ascii=False))
    return {"phase": "finish_v32", "ok": True, "sec": sec, "frames": fin_n,
            "up_mode": up_mode, "out_mb": out_mb, "tms": sw.out()}


# ---------------- 업로드·다운로드 A/B 마이크로벤치 (Phase 1 항목 3·4·5·10) ----------------
def upbench_v32(proj, tmp, inp):
    """같은 파일(기준 원본, 약 102MB)로 기존 단일 PUT과 S3 멀티파트 조합을 직접 비교.
    다운로드도 직렬 vs Range 병렬을 함께 계측. 테스트 객체는 끝나면 삭제."""
    t_begin = time.time()
    res = {"phase": "upbench_v32", "s3_enabled": _s3_client() is not None,
           "s3_endpoint": _S3_CACHED.get("ep"), "s3_diag": _S3_CACHED.get("diag"),
           "dl": [], "up": []}
    url = h29.signed_url(proj["source_path"], 7200)
    f = os.path.join(tmp, "payload.mp4")
    t = time.time(); h29.download_to(url, f)
    res["dl"].append({"cfg": "serial", "s": round(time.time() - t, 1)})
    res["size_mb"] = round(os.path.getsize(f) / 1e6, 1)
    for conc in (4, 8, 16):
        t = time.time(); download_to_par(url, f, conc=conc)
        res["dl"].append({"cfg": f"par{conc}", "s": round(time.time() - t, 1)})
    key = f"{proj['user_id']}/upbench_{int(t_begin)}.mp4"
    matrix = inp.get("matrix") or [
        ["put", 0, 0],
        ["s3", 8, 2], ["s3", 8, 4], ["s3", 8, 8],
        ["s3", 16, 2], ["s3", 16, 4], ["s3", 16, 8],
        ["s3", 32, 4], ["s3", 32, 8],
        ["s3", 64, 4], ["s3", 64, 8],
        ["put", 0, 0],
    ]
    c = _s3_client()
    for tag, pmb, conc in matrix:
        if tag == "s3" and c is None:
            continue
        if time.time() - t_begin > 680:
            res["truncated"] = True
            break
        t = time.time(); ok = True; err = ""
        try:
            if tag == "put":
                h29.upload_clip(key, f)
            else:
                s3_upload("videos-clips", key, f, pmb, conc, client=c)
        except Exception as e:
            ok = False; err = f"{type(e).__name__}: {e}"[:200]
        res["up"].append({"cfg": tag, "part_mb": pmb, "conc": conc,
                          "s": round(time.time() - t, 1), "ok": ok, "err": err})
    # 무결성 검증 1회: s3 업로드본 재다운로드 md5 == 로컬 md5
    if c is not None and time.time() - t_begin < 640:
        import hashlib, requests as rq
        try:
            s3_upload("videos-clips", key, f, 16, 8, client=c)
            g = os.path.join(tmp, "verify.mp4")
            r = rq.get(f"{h29.SB_URL}/storage/v1/object/videos-clips/{key}",
                       headers=h29.sb_headers(), stream=True, timeout=600)
            r.raise_for_status()
            with open(g, "wb") as fo:
                for ch in r.iter_content(1 << 20):
                    fo.write(ch)
            def _md5(fp):
                m = hashlib.md5()
                with open(fp, "rb") as fi:
                    for ch in iter(lambda: fi.read(1 << 20), b""):
                        m.update(ch)
                return m.hexdigest()
            res["checksum_ok"] = (_md5(f) == _md5(g))
        except Exception as e:
            res["checksum_ok"] = False
            res["checksum_err"] = f"{type(e).__name__}: {e}"[:200]
    try:
        import requests as rq
        rq.request("DELETE", f"{h29.SB_URL}/storage/v1/object/videos-clips",
                   headers=h29.sb_headers({"Content-Type": "application/json"}),
                   data=json.dumps({"prefixes": [key]}), timeout=60)
    except Exception:
        pass
    res["elapsed_s"] = round(time.time() - t_begin, 1)
    return res


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
            return finish_v32(proj, tmp, t0, int(inp.get("parts") or 0), inp.get("tms"),
                              stream=bool(inp.get("stream")), wait_s=int(inp.get("wait_s") or 1500),
                              deep_audit=bool(inp.get("deep_audit")))
        if phase == "upbench_v32":
            return upbench_v32(proj, tmp, inp)
        return {"error": f"알 수 없는 phase: {phase}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"[v32:{phase}] {type(e).__name__}: {e}"}
    finally:
        if hb: hb.set()
        shutil.rmtree(tmp, ignore_errors=True)
