# -*- coding: utf-8 -*-
"""V32 수동 검수 증거 생성 — v31(golden) vs v32 비교 자료를 아티팩트로 만든다.

산출물(clips/ 폴더):
- sbs_t{T}.mp4  : 시점 T부터 1.5초, 위(v31)/아래(v32) 상하 비교 (자막 밴드 확대)
- band_t{T}.png : 시점 T의 자막 밴드 crop 나란히 (왼쪽 v31 / 오른쪽 v32)
- boundary_b{B}.mp4 : 세그먼트 경계 ±0.5초 v32 단독 (깜빡임 검사용)
"""
import json, os, subprocess, sys, tempfile

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
BENCH_PID = "beac0001-0000-4000-8000-000000000031"
N, FPS, K = 5320, 30.0, int(os.environ.get("V32_K", "10"))
# 자막 활동 구간에서 고른 검사 시점(초) — 초반/중반/후반 + 밴드 y위치는 세로영상 하단부
TIMES = [12.0, 55.0, 96.0, 140.0, 168.0]
BAND = (0, 1200, 1080, 520)   # x,y,w,h — 자막 밴드 근방


def sbh():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}


def dl(path, dest):
    r = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                     headers=sbh(), stream=True, timeout=900)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)


def main():
    tmp = tempfile.mkdtemp()
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{BENCH_PID}", "select": "user_id"},
                     headers=sbh(), timeout=30)
    r.raise_for_status()
    uid = r.json()[0]["user_id"]
    a = os.path.join(tmp, "v31.mp4"); dl(f"{uid}/wm_v31_{BENCH_PID}.mp4", a)
    b = os.path.join(tmp, "v32.mp4"); dl(f"{uid}/wm_v32_{BENCH_PID}.mp4", b)
    os.makedirs("clips", exist_ok=True)
    x, y, w, h = BAND
    for t in TIMES:
        # 상하 비교 영상 (밴드 crop, 1.5초)
        out = f"clips/sbs_t{int(t)}.mp4"
        subprocess.run(["ffmpeg", "-v", "error",
                        "-ss", f"{t:.2f}", "-i", a, "-ss", f"{t:.2f}", "-i", b,
                        "-filter_complex",
                        f"[0:v]crop={w}:{h}:{x}:{y},drawtext=text='v31':fontcolor=yellow:fontsize=36:x=10:y=10[a];"
                        f"[1:v]crop={w}:{h}:{x}:{y},drawtext=text='v32':fontcolor=yellow:fontsize=36:x=10:y=10[b];"
                        f"[a][b]vstack", "-t", "1.5",
                        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", out, "-y"],
                       check=True)
        # 정지 프레임 비교 PNG
        out2 = f"clips/band_t{int(t)}.png"
        subprocess.run(["ffmpeg", "-v", "error",
                        "-ss", f"{t:.2f}", "-i", a, "-ss", f"{t:.2f}", "-i", b,
                        "-filter_complex",
                        f"[0:v]crop={w}:{h}:{x}:{y}[a];[1:v]crop={w}:{h}:{x}:{y}[b];[a][b]hstack",
                        "-vframes", "1", out2, "-y"], check=True)
    for s in (3, 5, 7):
        bd = s * N // K
        t = max(0.0, bd / FPS - 0.5)
        subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", b,
                        "-t", "1.0", "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                        f"clips/boundary_b{bd}.mp4", "-y"], check=True)
    print("[clips]", os.listdir("clips"))


if __name__ == "__main__":
    main()
