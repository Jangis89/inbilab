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
    N = h29.frame_count(work)
    if sw: sw.mark("cnt")
    expected = round(info["dur"] * info["fps"])
    if N and abs(N - expected) > max(5, expected * 0.02):
        work = os.path.join(tmp, "work.mp4")
        h29.run(["ffmpeg", "-v", "error"] + h29.hw_dec_args()
                + ["-i", src, "-vf", f"fps={info['fps']}", "-an"]
                + h29.hw_enc_args(12) + [work, "-y"])
        N = h29.frame_count(work)
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
        out.append(_h._scan_boxes2((arr[i], ref)))
    return out


def detect_sub_bands_shm(samples, W, H):
    """h29.detect_sub_bands_from과 동일 결과 — 샘플 전달만 공유메모리 (그룹핑은 순서 무관)."""
    global _V32_POOL
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
        hits = []; labs = []
        for pt in parts:
            for boxes, lb in pt:
                hits.extend(boxes); labs.extend(lb)
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
    # ±(6 + ceil((key_step-1)/2)) union — 키 간격의 절반만 넓혀 커버리지 보존 + 과도한 번짐 방지
    ks = max(1, int(key_step))
    ring = 6 + (ks - 1 + 1) // 2
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
                            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
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
            "counters": counters, "tms": sw.out(),
            "container_id": os.environ.get("MODAL_TASK_ID") or os.environ.get("HOSTNAME", "?"),
            "t_enter": round(t_enter, 3), "t_done": round(time.time(), 3)}


# ---------------- 단계: finish (CPU) ----------------
def _seg_exists(pid, name):
    import requests as _rq
    r = _rq.get(f"{h29.SB_URL}/storage/v1/object/videos-clips/{PFX}/{pid}/{name}",
                headers=h29.sb_headers({"Range": "bytes=0-0"}), timeout=20)
    return r.status_code in (200, 206)


def finish_v32(proj, tmp, t0, parts, tms_in=None, stream=False, wait_s=1500):
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
        counts[k] = h29.frame_count(seg_paths[k])
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
    fin_n = h29.frame_count(outp)
    if fin_n != N:
        raise RuntimeError(f"[v32] 최종 프레임 수 {fin_n} != N {N}")
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
                              stream=bool(inp.get("stream")), wait_s=int(inp.get("wait_s") or 1500))
        if phase == "upbench_v32":
            return upbench_v32(proj, tmp, inp)
        return {"error": f"알 수 없는 phase: {phase}"}
    except Exception as e:
        traceback.print_exc()
        return {"error": f"[v32:{phase}] {type(e).__name__}: {e}"}
    finally:
        if hb: hb.set()
        shutil.rmtree(tmp, ignore_errors=True)
