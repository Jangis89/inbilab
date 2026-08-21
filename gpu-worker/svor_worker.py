# -*- coding: utf-8 -*-
"""SVOR 스테이징 워커 (명세 G6) — /app/svor(공식 코드, Apache-2.0) 위에서 실행.

ops:
  smoke: 합성 81f 입력으로 파이프라인 검증 (load/runtime/VRAM 실측)
  roi:   storage의 (video, mask) 쌍을 처리해 out 경로로 업로드

원칙: BF16 · 20 steps · model_full_load 기본 (명세 G6.3, 양자화 금지 —
품질 비교 전 속도를 위해 품질을 낮추지 않는다). 문제 interval만 81-frame
window로 처리 (명세 G6.4) — window 분할은 호출측(ROI pack)이 담당.
"""
import gc
import os
import time

import cv2
import numpy as np
import requests
import torch

MODELS_DIR = "/vol/models"
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_ROLE", "")

_CACHE = {}  # lora_key -> pipeline


def _hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def _split(p):
    if ":" in p.split("/", 1)[0]:
        b, rest = p.split(":", 1)
        return b, rest
    return "videos-clips", p


def _download(path, dst):
    b, p = _split(path)
    with requests.get(f"{SB_URL}/storage/v1/object/{b}/{p}", headers=_hdr(),
                      stream=True, timeout=1800) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)
    return os.path.getsize(dst)


def _upload(src, path, ctype="video/mp4"):
    b, p = _split(path)
    with open(src, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/{b}/{p}",
                          headers=_hdr({"Content-Type": ctype,
                                        "x-upsert": "true"}),
                          data=f.read(), timeout=1800)
    r.raise_for_status()
    return os.path.getsize(src)


def _load_pipeline(lora="stage12", weight_dtype=torch.bfloat16):
    key = f"{lora}"
    if key in _CACHE:
        return _CACHE[key], 0.0
    t0 = time.time()
    from omegaconf import OmegaConf
    from transformers import AutoTokenizer
    from diffusers import FlowMatchEulerDiscreteScheduler
    from videox_fun.models import (AutoencoderKLWan, WanT5EncoderModel,
                                   VaceWanModel)
    from videox_fun.pipeline import SVORPipeline
    from videox_fun.utils.lora_utils import merge_lora
    from videox_fun.utils.utils import filter_kwargs

    config = OmegaConf.load("/app/svor/config/wan2.1/wan_civitai.yaml")
    base = os.path.join(MODELS_DIR, "Wan2.1-VACE-1.3B")
    tk = config["transformer_additional_kwargs"]
    transformer = VaceWanModel.from_pretrained(
        os.path.join(base, tk.get("transformer_subpath", "./")),
        transformer_additional_kwargs=OmegaConf.to_container(tk),
        low_cpu_mem_usage=True, torch_dtype=weight_dtype)
    vae = AutoencoderKLWan.from_pretrained(
        os.path.join(base, config["vae_kwargs"].get("vae_subpath")),
        additional_kwargs=OmegaConf.to_container(config["vae_kwargs"]),
    ).to(weight_dtype)
    tokenizer = AutoTokenizer.from_pretrained(
        os.path.join(base, config["text_encoder_kwargs"].get(
            "tokenizer_subpath")))
    text_encoder = WanT5EncoderModel.from_pretrained(
        os.path.join(base, config["text_encoder_kwargs"].get(
            "text_encoder_subpath")),
        additional_kwargs=OmegaConf.to_container(
            config["text_encoder_kwargs"]),
    ).to(weight_dtype).eval()
    scheduler = FlowMatchEulerDiscreteScheduler(
        **filter_kwargs(FlowMatchEulerDiscreteScheduler,
                        OmegaConf.to_container(config["scheduler_kwargs"])))
    pipe = SVORPipeline(transformer=transformer, vae=vae, tokenizer=tokenizer,
                        text_encoder=text_encoder, scheduler=scheduler)
    pipe.to(device="cuda")  # model_full_load (H100/H200 80GB+)
    if lora == "stage12":
        for lp in ("remove_model_stage1.safetensors",
                   "remove_model_stage2.safetensors"):
            pipe = merge_lora(pipe, os.path.join(MODELS_DIR, lp), 1.0)
    elif lora == "none":
        pass  # 순정 VACE-1.3B (후보 C)
    _CACHE[key] = pipe
    return pipe, time.time() - t0


PROMPT = "Remove the target and fill the content appropriately"
NEG = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
       "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
       "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，"
       "手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")


def _read_video(path, nmax):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 16
    frames = []
    while len(frames) < nmax:
        ret, fr = cap.read()
        if not ret:
            break
        frames.append(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
    cap.release()
    return frames, fps


def _muse(masks):
    """MUSE mask 전처리 A/B (명세 P4): 첫 프레임 anchor, 이후 4-frame 그룹
    temporal OR — VAE 시간축 압축단위(4)와 정렬. window 밖 union 없음."""
    out = [m.copy() for m in masks]
    n = len(masks)
    i = 1
    while i < n:
        grp = masks[i:i + 4]
        u = grp[0].copy()
        for m in grp[1:]:
            u = np.maximum(u, m)
        for j in range(i, min(i + 4, n)):
            out[j] = u
        i += 4
    return out


def _prep(frames, masks, video_length, size_hw, dilation):
    """predict_SVOR.process_video와 동일한 전처리 (frames/masks는 np RGB/GRAY)."""
    import scipy.ndimage
    H, W = size_hw
    if len(frames) < video_length:
        frames = frames + [frames[-1]] * (video_length - len(frames))
    frames = frames[:video_length]
    if len(masks) < video_length:
        masks = masks + [masks[-1]] * (video_length - len(masks))
    masks = masks[:video_length]
    fr_r = [cv2.resize(f, (W, H), interpolation=cv2.INTER_AREA)
            for f in frames]
    mk_r = []
    for m in masks:
        _, mb = cv2.threshold(m, 127, 255, cv2.THRESH_BINARY)
        if dilation > 0:
            mb = scipy.ndimage.binary_dilation(
                (mb > 0).astype(np.uint8), iterations=dilation
            ).astype(np.uint8) * 255
        mk_r.append(cv2.resize(mb, (W, H),
                               interpolation=cv2.INTER_NEAREST))
    v = (torch.stack([torch.from_numpy(f).permute(2, 0, 1) for f in fr_r])
         .permute(1, 0, 2, 3).unsqueeze(0).float())          # 1,C,T,H,W
    m = (torch.stack([torch.from_numpy(m) for m in mk_r])
         .unsqueeze(0).unsqueeze(0).float() / 255.0)          # 1,1,T,H,W
    v = v * (torch.tile(m, [1, 3, 1, 1, 1]) < 0.5) + 128.0 * (
        torch.tile(m, [1, 3, 1, 1, 1]) >= 0.5)
    v = v.div_(127.5).sub_(1.0)
    return v, m


def _fit_size(h, w, max_area):
    ar = h / w
    nh = round(np.sqrt(max_area * ar))
    nh = (nh + 15) // 16 * 16
    nw = round(np.sqrt(max_area / ar))
    nw = (nw + 15) // 16 * 16
    return nh, nw


def _run_pipe(pipe, frames, masks, ev):
    steps = int(ev.get("steps", 20))
    seed = int(ev.get("seed", 43))
    dilation = int(ev.get("dilation", 6))
    guidance = float(ev.get("guidance", 6.0))
    ctx = float(ev.get("context_scale", 1.0))
    max_area = int(ev.get("max_area", 720 * 1280))
    h0, w0 = frames[0].shape[:2]
    H, W = _fit_size(h0, w0, max_area)
    tcr = pipe.vae.config.temporal_compression_ratio
    vl = int((len(frames) - 1) // tcr * tcr) + 1
    v, m = _prep(frames, masks, vl, (H, W), dilation)
    gen = torch.Generator(device="cuda").manual_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    with torch.no_grad():
        sample = pipe(PROMPT, negative_prompt=NEG, height=H, width=W,
                      generator=gen, guidance_scale=guidance,
                      num_inference_steps=steps, video=v, mask_video=m,
                      context_scale=ctx).videos
    dt = time.time() - t0
    vram = torch.cuda.max_memory_allocated() / (1 << 30)
    # sample: 1,C,T,H,W in [0,1] → np frames RGB, 원 해상도로 복귀
    out = (sample[0].permute(1, 2, 3, 0).cpu().float().numpy() * 255
           ).clip(0, 255).astype(np.uint8)
    out = [cv2.resize(f, (w0, h0), interpolation=cv2.INTER_LANCZOS4)
           for f in out]
    return out, {"run_s": round(dt, 2), "vram_gb": round(vram, 2),
                 "size_hw": [H, W], "video_length": vl, "steps": steps,
                 "seed": seed, "dilation": dilation}


def _write_mp4(frames, path, fps):
    h, w = frames[0].shape[:2]
    tmp = path + ".raw.mp4"
    vw = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for f in frames:
        vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    vw.release()
    # 동일 encode control(명세 G3.3): x264 crf16 yuv420p로 통일
    os.system(f"ffmpeg -v error -y -i {tmp} -c:v libx264 -crf 16 "
              f"-pix_fmt yuv420p -preset medium {path}")
    os.remove(tmp)


def handle(ev: dict) -> dict:
    op = ev.get("op")
    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    if op == "smoke":
        # 합성 입력: 이동 텍스처 배경 + 자막형 사각 mask (81f, 720p 세로)
        rng = np.random.default_rng(7)
        base = rng.integers(40, 216, (1500, 900, 3), np.uint8)
        base = cv2.GaussianBlur(base, (0, 0), 3)
        frames, masks = [], []
        for t in range(81):
            ox, oy = 2 * t, t
            fr = base[oy:oy + 1280, ox:ox + 720].copy()
            cv2.putText(fr, "SMOKE TEST 0123", (60, 1100),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.6, (255, 255, 255), 4)
            mk = np.zeros((1280, 720), np.uint8)
            mk[1060:1130, 30:690] = 255
            frames.append(fr)
            masks.append(mk)
        pipe, load_s = _load_pipeline(ev.get("lora", "stage12"))
        out, met = _run_pipe(pipe, frames, masks, ev)
        tag = ev.get("tag", "smoke")
        local = f"/tmp/{tag}.mp4"
        _write_mp4(out, local, 16)
        dst = ev.get("out", f"bench-assets/svor/{tag}.mp4")
        n = _upload(local, dst)
        return {"ok": True, "op": op, "gpu": gpu, "load_s": round(load_s, 1),
                "out": dst, "bytes": n, **met}
    if op == "roi":
        vp, mp = "/tmp/in.mp4", "/tmp/mask.mp4"
        _download(ev["video"], vp)
        _download(ev["mask"], mp)
        nmax = int(ev.get("frames", 81))
        frames, fps = _read_video(vp, nmax)
        masks_rgb, _ = _read_video(mp, nmax)
        masks = [cv2.cvtColor(m, cv2.COLOR_RGB2GRAY) for m in masks_rgb]
        if int(ev.get("muse", 0)):
            masks = _muse(masks)
        pipe, load_s = _load_pipeline(ev.get("lora", "stage12"))
        out, met = _run_pipe(pipe, frames, masks, ev)
        local = "/tmp/out.mp4"
        _write_mp4(out, local, fps)
        n = _upload(local, ev["out"])
        gc.collect()
        torch.cuda.empty_cache()
        return {"ok": True, "op": op, "gpu": gpu, "load_s": round(load_s, 1),
                "out": ev["out"], "bytes": n, "fps": fps, **met}
    if op == "flowbench":
        return _flowbench(ev, gpu)
    return {"ok": False, "error": f"unknown op {op}"}


# ---------------- GPU flow 벤치 (명세 P5) ----------------
def _flow_dis_half(g1, g2, _st=[None]):
    if _st[0] is None:
        d = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        _st[0] = d
    h, w = g1.shape
    s1 = cv2.resize(g1, (w // 2, h // 2))
    s2 = cv2.resize(g2, (w // 2, h // 2))
    f = _st[0].calc(s1, s2, None)
    f = cv2.resize(f, (w, h), interpolation=cv2.INTER_LINEAR) * 2.0
    return f


_TVRAFT = {}


def _flow_tvraft(im1, im2, small=True):
    """torchvision RAFT (small/large). im: BGR uint8 → flow HxWx2 (px)."""
    import torch
    import torch.nn.functional as Fn
    key = "s" if small else "l"
    if key not in _TVRAFT:
        from torchvision.models.optical_flow import (
            raft_small, Raft_Small_Weights, raft_large, Raft_Large_Weights)
        if small:
            m = raft_small(weights=Raft_Small_Weights.DEFAULT)
        else:
            m = raft_large(weights=Raft_Large_Weights.DEFAULT)
        _TVRAFT[key] = m.eval().cuda()
    m = _TVRAFT[key]
    h, w = im1.shape[:2]
    h8, w8 = (h + 7) // 8 * 8, (w + 7) // 8 * 8

    def prep(im):
        t = torch.from_numpy(im[:, :, ::-1].copy()).permute(2, 0, 1)[None] \
            .float().cuda() / 127.5 - 1.0
        return Fn.pad(t, (0, w8 - w, 0, h8 - h))
    with torch.no_grad():
        fl = m(prep(im1), prep(im2))[-1][0, :, :h, :w]
    return fl.permute(1, 2, 0).cpu().numpy()


_SEARAFT = {}


def _flow_searaft(im1, im2):
    """SEA-RAFT (HF: MemorySlices/Tartan-C-T-TSKH-spring540x960-M)."""
    import sys
    import torch
    import torch.nn.functional as Fn
    if "m" not in _SEARAFT:
        for p in ("/app/searaft/core", "/app/searaft"):
            if p not in sys.path:
                sys.path.insert(0, p)
        import json as _json
        from argparse import Namespace
        from raft import RAFT
        cfgp = "/app/searaft/config/eval/spring-M.json"
        args = Namespace(**_json.load(open(cfgp)))
        m = RAFT.from_pretrained(
            "MemorySlices/Tartan-C-T-TSKH-spring540x960-M", args=args)
        _SEARAFT["m"] = m.eval().cuda()
        _SEARAFT["args"] = args
    m = _SEARAFT["m"]
    h, w = im1.shape[:2]
    h8, w8 = (h + 7) // 8 * 8, (w + 7) // 8 * 8

    def prep(im):
        t = torch.from_numpy(im[:, :, ::-1].copy()).permute(2, 0, 1)[None] \
            .float().cuda()
        return Fn.pad(t, (0, w8 - w, 0, h8 - h))
    with torch.no_grad():
        out = m(prep(im1), prep(im2), iters=_SEARAFT["args"].iters,
                test_mode=True)
        fl = out["flow"][-1] if isinstance(out, dict) else out[-1]
    return fl[0, :, :h, :w].permute(1, 2, 0).cpu().numpy()


def _flowbench(ev, gpu):
    """ROI 팩에 대해 flow 후보별 품질·속도 측정 (명세 5.4).

    지표: fwd-bwd round-trip 오차, mask 내 valid coverage(왕복<1.5px &
    참조화소가 mask 밖), ring 광도오차(warp 정합성 ghost proxy),
    runtime/pair, VRAM. offsets (3,10,20).
    """
    import torch
    vp, mp = "/tmp/fb_in.mp4", "/tmp/fb_mask.mp4"
    _download(ev["video"], vp)
    _download(ev["mask"], mp)
    nmax = int(ev.get("frames", 81))
    frames, fps = _read_video(vp, nmax)
    frames = [cv2.cvtColor(f, cv2.COLOR_RGB2BGR) for f in frames]  # BGR 작업
    masks_rgb, _ = _read_video(mp, nmax)
    masks = [cv2.cvtColor(m, cv2.COLOR_RGB2GRAY) for m in masks_rgb]
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]
    engines = ev.get("engines", ["dis_half", "raft_small", "raft_large",
                                 "sea_raft"])
    offsets = ev.get("offsets", [3, 10, 20])
    ts = list(range(0, len(frames), int(ev.get("step", 6))))
    H, W = frames[0].shape[:2]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    res = {}
    for eng in engines:
        try:
            torch.cuda.reset_peak_memory_stats()
            errs, covs, ghosts, times = [], [], [], []
            npairs = 0
            for t in ts:
                m = masks[t] > 127
                if not m.any():
                    continue
                for k in offsets:
                    r = t + k if t + k < len(frames) else t - k
                    if r < 0 or r == t:
                        continue
                    t0 = time.time()
                    if eng == "dis_half":
                        fw = _flow_dis_half(grays[t], grays[r])
                        bw = _flow_dis_half(grays[r], grays[t])
                    elif eng == "raft_small":
                        fw = _flow_tvraft(frames[t], frames[r], True)
                        bw = _flow_tvraft(frames[r], frames[t], True)
                    elif eng == "raft_large":
                        fw = _flow_tvraft(frames[t], frames[r], False)
                        bw = _flow_tvraft(frames[r], frames[t], False)
                    elif eng == "sea_raft":
                        fw = _flow_searaft(frames[t], frames[r])
                        bw = _flow_searaft(frames[r], frames[t])
                    else:
                        raise ValueError(eng)
                    dt = (time.time() - t0) / 2.0
                    times.append(dt)
                    npairs += 1
                    # round-trip: p + fw(p) + bw(p+fw(p)) ≈ p
                    mx = xx + fw[..., 0]
                    my = yy + fw[..., 1]
                    bwx = cv2.remap(bw[..., 0], mx, my, cv2.INTER_LINEAR)
                    bwy = cv2.remap(bw[..., 1], mx, my, cv2.INTER_LINEAR)
                    rt = np.sqrt((fw[..., 0] + bwx) ** 2
                                 + (fw[..., 1] + bwy) ** 2)
                    errs.append(float(np.median(rt[m])))
                    # 참조 mask를 fw로 샘플: 참조화소가 mask 밖이어야 실화소
                    rm = (masks[r] > 127).astype(np.float32)
                    rm_s = cv2.remap(rm, mx, my, cv2.INTER_LINEAR)
                    valid = (rt < 1.5) & (rm_s < 0.25)
                    covs.append(float(valid[m].mean()))
                    # ghost proxy: mask 밖 ring에서 warp된 참조 vs 실제
                    warped = cv2.remap(frames[r], mx, my, cv2.INTER_LINEAR)
                    ring = cv2.dilate(m.astype(np.uint8),
                                      np.ones((25, 25), np.uint8)).astype(bool) & (~m)
                    okr = ring & (rt < 1.5)
                    if okr.any():
                        ghosts.append(float(np.abs(
                            warped.astype(np.float32)
                            - frames[t].astype(np.float32))[okr].mean()))
            res[eng] = {
                "pairs": npairs,
                "rt_err_med_px": round(float(np.median(errs)), 3) if errs else None,
                "real_pixel_coverage": round(float(np.mean(covs)), 4) if covs else None,
                "residual_hole_ratio": round(1.0 - float(np.mean(covs)), 4) if covs else None,
                "ghost_photo_err": round(float(np.mean(ghosts)), 2) if ghosts else None,
                "s_per_flow": round(float(np.mean(times)), 3) if times else None,
                "vram_gb": round(torch.cuda.max_memory_allocated() / (1 << 30), 2)}
        except Exception as e:  # noqa: BLE001 — 엔진별 실패 격리
            res[eng] = {"error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "op": "flowbench", "gpu": gpu, "roi": ev.get("roi"),
            "frames": len(frames), "size": [H, W], "results": res}
