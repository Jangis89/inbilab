# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 SVOR 스테이징 — 별도 Modal 앱 (명세 G6.1)
# 현 V32 staging(inbilab-wm-gpu-v32-speed-staging)과 완전 분리:
#   - dependency 충돌 방지 (diffusers 0.31 / transformers 4.46.2 pin)
#   - 운영 v29 무접촉, permanent GPU 0 (모두 on-demand)
# 구성: 가중치는 Volume(svor-models)에 다운로드 + SBOM sha256 대조,
#       H100!/H200 각각 고정 함수로 벤치 결과 섞임 방지 (명세 G5.2/G6.2)
# 배포: .github/workflows/svor-staging.yml (workflow_dispatch 전용)
# ============================================================
import os
import time

import modal

APP_NAME = "inbilab-wm-svor-staging"
HERE = os.path.dirname(os.path.abspath(__file__))
SVOR_COMMIT = "df1fe23248c46477aea665c0f116fff91184f26d"  # SBOM 고정
VOL_NAME = "svor-models"
MODELS_DIR = "/vol/models"

app = modal.App(APP_NAME)
vol = modal.Volume.from_name(VOL_NAME, create_if_missing=True)

gpu_image = (
    modal.Image.from_registry("pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime")
    .apt_install("ffmpeg", "git")
    .pip_install(
        # SVOR requirements.txt 준수 (gradio/flask 등 데모 전용은 제외)
        "diffusers==0.31.0", "transformers==4.46.2", "accelerate>=0.25.0",
        "omegaconf", "safetensors", "einops", "scipy",
        "opencv-python-headless==4.10.0.84", "numpy==1.26.4",
        "imageio[ffmpeg]", "decord", "scikit-image", "sentencepiece",
        "huggingface_hub", "requests", "ftfy",
    )
    .run_commands(
        f"git clone https://github.com/xiaomi-research/SVOR /app/svor && "
        f"cd /app/svor && git checkout {SVOR_COMMIT}"
    )
    .env({"PYTHONUNBUFFERED": "1", "HF_HUB_ENABLE_HF_TRANSFER": "0"})
)
gpu_image = gpu_image.add_local_file(
    os.path.join(HERE, "svor_worker.py"), "/app/svor_worker.py")

cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("huggingface_hub", "requests")
    .env({"PYTHONUNBUFFERED": "1"})
)

_secrets = [modal.Secret.from_name("inbilab-supabase")]

# SBOM 대조값 (docs/RC4_MODEL_SBOM.md — 불일치 시 즉시 실패)
SBOM_SHA256 = {
    "Wan2.1-VACE-1.3B/diffusion_pytorch_model.safetensors":
        "c46a6f5f7d32c453c3983bbc59761ea41cd02ad584fb55d1a7ee2b76145847a2",
    "Wan2.1-VACE-1.3B/Wan2.1_VAE.pth":
        "38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981",
    "Wan2.1-VACE-1.3B/models_t5_umt5-xxl-enc-bf16.pth":
        "7cace0da2b446bbbbc57d031ab6cf163a3d59b366da94e5afe36745b746fd81d",
    "remove_model_stage1.safetensors":
        "7846f8a188aa88904f55bcf6c49f0cbb9aaca2da4669dca50af75990ac6beb15",
    "remove_model_stage2.safetensors":
        "fd52a47c4c49f5f2d73e2b823c32ab245030060ead4c4ce3aa4d7198fc197b9d",
}


def _sha256(path, chunk=1 << 22):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


@app.function(image=cpu_image, cpu=8.0, memory=16384, timeout=3600,
              volumes={"/vol": vol})
def download_models(event: dict) -> dict:
    """가중치를 Volume에 내려받고 SBOM sha256 전수 대조."""
    from huggingface_hub import snapshot_download, hf_hub_download
    os.makedirs(MODELS_DIR, exist_ok=True)
    t0 = time.time()
    snapshot_download(
        "Wan-AI/Wan2.1-VACE-1.3B",
        revision="574e6a744642ce3bee319afc31496b88bde8aac4",
        local_dir=os.path.join(MODELS_DIR, "Wan2.1-VACE-1.3B"),
        allow_patterns=["*.json", "*.safetensors", "*.pth", "*.model",
                        "google/**", "config*"],
    )
    for f in ("remove_model_stage1.safetensors",
              "remove_model_stage2.safetensors"):
        hf_hub_download(
            "HigherHu/SVOR", f,
            revision="a2b23c835a6c046247ea1ed2aa83d075853e5ac4",
            local_dir=MODELS_DIR)
    results, bad = {}, []
    for rel, want in SBOM_SHA256.items():
        p = os.path.join(MODELS_DIR, rel)
        got = _sha256(p) if os.path.exists(p) else "MISSING"
        results[rel] = {"sha256": got, "ok": got == want}
        if got != want:
            bad.append(rel)
    vol.commit()
    return {"ok": not bad, "bad": bad, "results": results,
            "elapsed_s": round(time.time() - t0, 1)}


def _run(event):
    import sys
    for p in ("/app/svor", "/app"):
        if p not in sys.path:
            sys.path.insert(0, p)
    os.chdir("/app/svor")
    import svor_worker
    return svor_worker.handle(event)


@app.function(image=gpu_image, gpu="H100!", cpu=8.0, memory=65536,
              timeout=3600, retries=0, secrets=_secrets,
              volumes={"/vol": vol}, scaledown_window=120)
def svor_h100(event: dict) -> dict:
    t0 = time.time()
    out = _run(event)
    out["__fn"] = "svor_h100"
    out["__exec_ms"] = int((time.time() - t0) * 1000)
    return out


@app.function(image=gpu_image, gpu="H200", cpu=8.0, memory=65536,
              timeout=3600, retries=0, secrets=_secrets,
              volumes={"/vol": vol}, scaledown_window=120)
def svor_h200(event: dict) -> dict:
    t0 = time.time()
    out = _run(event)
    out["__fn"] = "svor_h200"
    out["__exec_ms"] = int((time.time() - t0) * 1000)
    return out
