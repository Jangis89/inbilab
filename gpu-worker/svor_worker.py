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
        pipe, load_s = _load_pipeline(ev.get("lora", "stage12"))
        out, met = _run_pipe(pipe, frames, masks, ev)
        local = "/tmp/out.mp4"
        _write_mp4(out, local, fps)
        n = _upload(local, ev["out"])
        gc.collect()
        torch.cuda.empty_cache()
        return {"ok": True, "op": op, "gpu": gpu, "load_s": round(load_s, 1),
                "out": ev["out"], "bytes": n, "fps": fps, **met}
    return {"ok": False, "error": f"unknown op {op}"}
