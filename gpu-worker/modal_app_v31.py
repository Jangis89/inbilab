# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 V31 스테이징 — Modal 앱 (운영 inbilab-wm-gpu와 완전 분리)
# 자원 분리: CPU plan / GPU segment / CPU finish (+ GPU warm)
# 배포: .github/workflows/modal-deploy-v31-staging.yml (workflow_dispatch 전용)
# 호출: 공개 endpoint 없음 — Modal SDK(Function.from_name)로만 호출 (벤치마크 스크립트)
# ============================================================
import os
import time
import modal

APP_NAME = "inbilab-wm-gpu-v31-staging"
HERE = os.path.dirname(os.path.abspath(__file__))

app = modal.App(APP_NAME)

_local_files = [
    ("handler.py", "/app/handler.py"),
    ("handler_v31.py", "/app/handler_v31.py"),
    ("pipeline_minimax_remover.py", "/app/pipeline_minimax_remover.py"),
    ("transformer_minimax_remover.py", "/app/transformer_minimax_remover.py"),
]

# ---------- GPU 이미지: 운영 v29와 동일 구성 + v31 코드 ----------
gpu_image = (
    modal.Image.from_registry("pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(os.path.join(HERE, "requirements.txt"))
    .env({
        "MODEL_DIR": "/models",
        "PYTHONUNBUFFERED": "1",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,video",
        "WM_BACKEND_NAME": "modal-v31",
    })
    .run_commands(
        "python -c \"from huggingface_hub import snapshot_download; "
        "snapshot_download('zibojia/minimax-remover', local_dir='/models', "
        "allow_patterns=['vae/*','transformer/*','scheduler/*'])\""
    )
)
for src, dst in _local_files:
    gpu_image = gpu_image.add_local_file(os.path.join(HERE, src), dst)

# ---------- CPU 이미지: 모델·torch 없음 (plan/finish 전용) ----------
cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install("numpy==1.26.4", "opencv-python-headless==4.10.0.84", "requests")
    .env({"PYTHONUNBUFFERED": "1", "WM_BACKEND_NAME": "modal-v31",
          "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
          "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1"})
)
for src, dst in _local_files:
    cpu_image = cpu_image.add_local_file(os.path.join(HERE, src), dst)

_secrets = [modal.Secret.from_name("inbilab-supabase")]


def _enter(app_dir="/app"):
    import sys
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    os.chdir(app_dir)


@app.function(image=cpu_image, cpu=16.0, memory=65536, ephemeral_disk=65536,
              timeout=1800, secrets=_secrets)
def plan_v31_cpu(event: dict) -> dict:
    _enter()
    import cv2
    cv2.setNumThreads(1)
    t0 = time.time()
    from handler_v31 import handler_v31
    out = handler_v31(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "plan_v31_cpu"
    return out


@app.function(image=gpu_image, gpu="L40S", cpu=8.0, memory=65536, ephemeral_disk=65536,
              timeout=1800, max_containers=32, retries=0, secrets=_secrets)
def segment_v31_gpu(event: dict) -> dict:
    _enter()
    t0 = time.time()
    from handler_v31 import handler_v31
    out = handler_v31(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "segment_v31_gpu"
    return out


@app.function(image=cpu_image, cpu=8.0, memory=16384, ephemeral_disk=32768,
              timeout=900, secrets=_secrets)
def finish_v31_cpu(event: dict) -> dict:
    _enter()
    import cv2
    cv2.setNumThreads(1)
    t0 = time.time()
    from handler_v31 import handler_v31
    out = handler_v31(event)
    if isinstance(out, dict):
        out["__exec_ms"] = int((time.time() - t0) * 1000)
        out["__fn"] = "finish_v31_cpu"
    return out


@app.function(image=gpu_image, gpu="L40S", cpu=4.0, memory=32768,
              timeout=600, max_containers=32, retries=0, secrets=_secrets)
def warm_v31_gpu() -> dict:
    _enter()
    from handler_v31 import warm_v31
    return warm_v31()
