# -*- coding: utf-8 -*-
"""V32 품질 게이트 (명세 18장) — V31 결과를 golden 기준선으로 비교.

하드 게이트: 프레임 수 5,320 / 해상도 / FPS / 오디오 존재 / PSNR ≥ 35 / SSIM ≥ 0.98
리포트: 세그먼트 경계 깜빡임(경계 ±3프레임 연속 프레임 차이의 급증 여부)
"""
import json, os, re, subprocess, sys, tempfile

import numpy as np
import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
BENCH_PID = "beac0001-0000-4000-8000-000000000031"
EXPECT_N = 5320
K_DEFAULT = int(os.environ.get("V32_K", "10"))


def sbh():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}


def storage_get(bucket, path, dest):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh(), stream=True, timeout=900)
    r.raise_for_status()
    n = 0
    with open(dest, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch); n += len(ch)
    return n


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames,avg_frame_rate,width,height:format=duration",
         "-of", "json", path], capture_output=True, text=True).stdout
    j = json.loads(out)
    a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                        "-show_entries", "stream=codec_name", "-of", "json", path],
                       capture_output=True, text=True).stdout
    st = j["streams"][0]
    return {"frames": int(st.get("nb_read_frames") or 0), "w": st.get("width"),
            "h": st.get("height"), "fps": st.get("avg_frame_rate"),
            "dur": round(float(j.get("format", {}).get("duration") or 0), 2),
            "audio": [s.get("codec_name") for s in json.loads(a).get("streams", [])]}


def metric(a, b, flt, rex):
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", a, "-i", b,
                        "-lavfi", f"[0:v][1:v]{flt}", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(rex, p.stderr)
    return float(m.group(1)) if m else None


def boundary_flicker(path, fps, n, k):
    """각 세그먼트 경계 b 주변 6프레임의 연속 프레임 평균 절대차 → 경계에서의 급증 여부"""
    res = []
    for s in range(1, k):
        b = s * n // k
        t = max(0.0, (b - 3 - 0.5) / fps)
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.4f}", "-i", path,
                            "-vframes", "7", "-vf", "scale=270:480", "-f", "rawvideo",
                            "-pix_fmt", "gray", "-"], capture_output=True)
        buf = np.frombuffer(p.stdout, np.uint8)
        if len(buf) < 270 * 480 * 7:
            res.append({"b": b, "err": "decode_short"}); continue
        fr = buf[:270 * 480 * 7].reshape(7, 480, 270).astype(np.int16)
        diffs = [float(np.abs(fr[i + 1] - fr[i]).mean()) for i in range(6)]
        # 경계 지점(3→4번째 프레임)의 차이가 주변 평균의 3배를 넘으면 flag
        around = (sum(diffs) - diffs[3]) / 5.0
        res.append({"b": b, "boundary_diff": round(diffs[3], 2),
                    "around_mean": round(around, 2),
                    "flag": bool(diffs[3] > max(3.0, around * 3.0))})
    return res


def main():
    tmp = tempfile.mkdtemp()
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{BENCH_PID}", "select": "user_id"},
                     headers=sbh(), timeout=30)
    r.raise_for_status()
    uid = r.json()[0]["user_id"]
    v32p = os.path.join(tmp, "v32.mp4")
    n1 = storage_get("videos-clips", f"{uid}/wm_v32_{BENCH_PID}.mp4", v32p)
    print(f"[verify32] v32 다운로드 {n1/1e6:.1f}MB")
    v31p = os.path.join(tmp, "v31.mp4")
    n2 = storage_get("videos-clips", f"{uid}/wm_v31_{BENCH_PID}.mp4", v31p)
    print(f"[verify32] v31 golden 다운로드 {n2/1e6:.1f}MB")

    p1, p2 = probe(v32p), probe(v31p)
    print("[verify32] v32:", json.dumps(p1, ensure_ascii=False))
    print("[verify32] v31:", json.dumps(p2, ensure_ascii=False))

    hard = []
    if p1["frames"] != EXPECT_N: hard.append(f"프레임 {p1['frames']} != {EXPECT_N}")
    if not p1["audio"]: hard.append("오디오 없음")
    if (p1["w"], p1["h"]) != (p2["w"], p2["h"]): hard.append("해상도 불일치")
    def _f(v):
        return (lambda a, b: a / b)(*map(float, str(v).split("/"))) if "/" in str(v) else float(v)
    if abs(_f(p1["fps"]) - _f(p2["fps"])) > 0.01:
        hard.append(f"FPS 불일치 {p1['fps']} vs {p2['fps']}")

    psnr = ssim = None
    if p1["frames"] == p2["frames"]:
        psnr = metric(v32p, v31p, "psnr", r"average:([\d.]+|inf)")
        ssim = metric(v32p, v31p, "ssim", r"All:([\d.]+)")
        if psnr is not None and psnr < 35.0: hard.append(f"PSNR {psnr} < 35")
        if ssim is not None and ssim < 0.98: hard.append(f"SSIM {ssim} < 0.98")
    else:
        hard.append("프레임 수 불일치로 PSNR 생략")

    fps_num = _f(p1["fps"])
    bnd = boundary_flicker(v32p, fps_num, EXPECT_N, K_DEFAULT)
    bnd_ref = boundary_flicker(v31p, fps_num, EXPECT_N, K_DEFAULT)
    # v31(golden)에도 같은 패턴이 있으면 추출 특성이지 v32 결함이 아님 → 양쪽 비교로 판정
    flags = []
    for a, b in zip(bnd, bnd_ref):
        if a.get("err") or b.get("err"):
            continue
        if a["flag"] and not b["flag"]:
            flags.append(a)
    print("[verify32] 경계(v32):", json.dumps(bnd, ensure_ascii=False))
    print("[verify32] 경계(v31):", json.dumps(bnd_ref, ensure_ascii=False))

    summary = {"psnr_vs_v31": psnr, "ssim_vs_v31": ssim,
               "boundary_flags": len(flags), "hard_fail": hard}
    print("\n[VERIFY32]", json.dumps(summary, ensure_ascii=False))
    if hard or flags:
        print("[verify32] 게이트 실패 ❌")
        sys.exit(1)
    print("[verify32] 품질 게이트 통과 ✅")


if __name__ == "__main__":
    main()
