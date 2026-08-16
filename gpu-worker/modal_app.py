# -*- coding: utf-8 -*-
# ============================================================
# 인비랩 자막·워터마크 제거 일꾼 — Modal 판 (v1)
# 기존 RunPod 일꾼(handler.py)을 코드 수정 없이 그대로 실행한다.
# 감시원(index.js)이 쓰던 RunPod API 모양(/run, /status/{id}, /cancel/{id})을
# 그대로 흉내 내는 창구를 함께 제공 → 감시원 쪽 변경 최소화.
# 배포: GitHub Actions가 `modal deploy gpu-worker/modal_app.py` 실행 (자동)
# ============================================================
import os
import time
import modal

APP_NAME = "inbilab-wm-gpu"
HERE = os.path.dirname(os.path.abspath(__file__))

app = modal.App(APP_NAME)

# ---------- 일꾼 이미지: RunPod Dockerfile과 동일 구성 ----------
image = (
    modal.Image.from_registry("pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(os.path.join(HERE, "requirements.txt"))
    .env({
        "MODEL_DIR": "/models",
        "PYTHONUNBUFFERED": "1",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility,video",
    })
    # 모델 가중치를 이미지 안에 미리 내려받기 (부팅 시 다운로드 0 → 빠른 시동)
    .run_commands(
        "python -c \"from huggingface_hub import snapshot_download; "
        "snapshot_download('zibojia/minimax-remover', local_dir='/models', "
        "allow_patterns=['vae/*','transformer/*','scheduler/*'])\""
    )
    .add_local_file(os.path.join(HERE, "handler.py"), "/app/handler.py")
    .add_local_file(os.path.join(HERE, "transformer_minimax_remover.py"), "/app/transformer_minimax_remover.py")
    .add_local_file(os.path.join(HERE, "pipeline_minimax_remover.py"), "/app/pipeline_minimax_remover.py")
)

# ---------- GPU 일꾼 본체 ----------
@app.function(
    image=image,
    gpu="L40S",                # 48GB 데이터센터급
    timeout=2400,              # 단계당 최대 40분 (감시원 capMs와 동일)
    max_containers=20,         # 동시 일꾼 최대 20 (RunPod과 동일 규모)
    retries=0,                 # 재시도는 감시원이 담당 (이중 재시도 방지)
    secrets=[modal.Secret.from_name("inbilab-supabase")],
)
def process(event: dict) -> dict:
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    os.chdir("/app")
    t0 = time.time()
    from handler import handler   # 기존 일꾼 프로그램 그대로
    out = handler(event)
    try:
        if isinstance(out, dict):
            out["__exec_ms"] = int((time.time() - t0) * 1000)  # 지각 감지 학습용 실행 시간
    except Exception:
        pass
    return out


# ---------- RunPod 모양 API 창구 (감시원이 쓰는 /run /status /cancel 흉내) ----------
web_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]")

@app.function(image=web_image, timeout=120)
@modal.concurrent(max_inputs=100)
@modal.asgi_app(requires_proxy_auth=True)   # Modal-Key / Modal-Secret 헤더 필요 (무단 사용 차단)
def api():
    from fastapi import FastAPI

    web = FastAPI()

    @web.post("/run")
    async def run(body: dict):
        event = {"input": (body or {}).get("input") or {}}
        call = process.spawn(event)
        return {"id": call.object_id, "status": "IN_QUEUE"}

    @web.get("/status/{call_id}")
    async def status(call_id: str):
        try:
            fc = modal.FunctionCall.from_id(call_id)
        except Exception:
            return {"id": call_id, "status": "FAILED", "error": "unknown call id"}
        try:
            out = fc.get(timeout=0)
            return {"id": call_id, "status": "COMPLETED", "output": out}
        except TimeoutError:
            # Modal은 대기/실행을 구분해 주지 않으므로 실행 중으로 취급
            return {"id": call_id, "status": "IN_PROGRESS"}
        except Exception as e:
            return {"id": call_id, "status": "FAILED", "error": str(e)[:300]}

    @web.post("/cancel/{call_id}")
    async def cancel(call_id: str):
        try:
            modal.FunctionCall.from_id(call_id).cancel()
            return {"id": call_id, "status": "CANCELLED"}
        except Exception as e:
            return {"id": call_id, "status": "FAILED", "error": str(e)[:300]}

    @web.get("/health")
    async def health():
        return {"ok": True, "app": APP_NAME}

    return web
