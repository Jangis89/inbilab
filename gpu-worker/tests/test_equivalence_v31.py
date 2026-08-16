# -*- coding: utf-8 -*-
"""V31 단계 2 게이트: 정확 구간 해독·crop 동일성 검증 (byte equality).

GitHub Actions 러너(CPU)에서 실행. 기준 영상은 Supabase에서 service role로 내려받는다.
통과 기준(명세 11.5/9.6): shape·프레임 수·모든 RGB byte 동일. 실패 시 exit 1.
"""
import json, os, random, subprocess, sys, tempfile

import numpy as np
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("SUPABASE_URL", "")
import handler as h29          # noqa: E402
import handler_v31 as v31      # noqa: E402

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
SRC_PATH = "4117e902-3396-4b14-aea0-957b326ab563/1786760197372_s94d16lfj6.mp4"  # 감사 기준 177초 영상
W, H, FPS, N = 1080, 1920, 30, 5320
random.seed(31)


def fetch_video(dest):
    r = requests.get(f"{SB_URL}/storage/v1/object/videos-source/{SRC_PATH}",
                     headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"},
                     stream=True, timeout=600)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)


def test_range_decode_equality(path):
    """순차(0부터) 해독 vs 정확 구간 해독 — 무작위 100+ 프레임 byte 동일."""
    ranges = sorted({(random.randrange(0, N - 40), random.randrange(3, 12)) for _ in range(14)})
    need = {}
    for f0, ln in ranges:
        for i in range(f0, min(N, f0 + ln)):
            need[i] = None
    print(f"[equiv] 기준 프레임 {len(need)}개 순차 해독 수집 중…")
    i = 0
    for fr in h29.stream_frames(path, W, H, stop_after=max(need) + 1):
        if i in need:
            need[i] = fr.copy()
        i += 1
    assert all(v is not None for v in need.values()), "순차 해독에서 기준 프레임 누락"
    checked = 0
    for f0, ln in ranges:
        f1 = min(N, f0 + ln)
        got = list(v31.stream_frames_range(path, W, H, f0, f1, FPS, hw=False))
        assert len(got) == f1 - f0, f"range({f0},{f1}) 프레임 수 {len(got)}"
        for j, fr in enumerate(got):
            ref = need[f0 + j]
            assert fr.shape == ref.shape, f"shape 불일치 @{f0 + j}"
            assert np.array_equal(fr, ref), f"RGB byte 불일치 @frame {f0 + j} (range {f0}-{f1})"
            checked += 1
    print(f"[equiv] range decode OK — {checked}프레임 byte 동일")


def test_crop_range_equality(path):
    """v29 crop 스트림(0부터) vs v31 구간 crop — 홀짝 좌표 섞은 영역들 byte 동일."""
    regs = []
    for _ in range(10):
        x = random.randrange(0, 200) | random.randrange(0, 2)   # 홀·짝 섞기
        y = random.randrange(1200, 1500)
        w = random.randrange(301, 880)
        hgt = random.randrange(81, 260)
        if x + w > W: w = W - x
        if y + hgt > H: hgt = H - y
        regs.append((x, y, w, hgt))
    f0 = random.randrange(100, 400)
    ln = 8
    for (x, y, w, hgt) in regs:
        ref = []
        i = 0
        for fr in h29._stream_crop(path, x, y, w, hgt):
            if i >= f0:
                ref.append(fr.copy())
            i += 1
            if i >= f0 + ln:
                break
        got = v31.read_crop_range(path, x, y, w, hgt, f0, f0 + ln, FPS, hw=False)
        assert len(got) == len(ref) == ln
        for j in range(ln):
            assert np.array_equal(got[j], ref[j]), f"crop({x},{y},{w},{hgt}) byte 불일치 @{f0 + j}"
    print(f"[equiv] crop range OK — {len(regs)}개 영역 × {ln}프레임 byte 동일")


def test_crop_order_ab(path):
    """9.6 crop 순서 A/B: 기존 format=rgb24,crop vs crop=..:exact=1,format=rgb24."""
    mismatch = 0
    cases = [(101, 1401, 877, 143), (100, 1400, 878, 144), (33, 1333, 501, 99), (0, 0, 1080, 200)]
    for (x, y, w, hgt) in cases:
        old = subprocess.run(["ffmpeg", "-v", "error", "-i", path,
                              "-vf", f"format=rgb24,crop={w}:{hgt}:{x}:{y}",
                              "-vframes", "3", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True).stdout
        new = subprocess.run(["ffmpeg", "-v", "error", "-i", path,
                              "-vf", f"crop={w}:{hgt}:{x}:{y}:exact=1,format=rgb24",
                              "-vframes", "3", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True).stdout
        same = old == new and len(old) == w * hgt * 3 * 3
        print(f"[A/B] crop({x},{y},{w},{hgt}) exact=1 동일성: {same}")
        if not same:
            mismatch += 1
    # A/B는 정보 수집용 — 불일치면 기존 방식 유지(명세 9.6). 실패로 처리하지 않음.
    print(f"[A/B] exact=1 불일치 {mismatch}/{len(cases)} → {'기존 방식 유지' if mismatch else '후보 채택 가능(속도 비교 필요)'}")


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "bench.mp4")
    print("[equiv] 기준 영상 다운로드…")
    fetch_video(path)
    test_range_decode_equality(path)
    test_crop_range_equality(path)
    test_crop_order_ab(path)
    print("\n[equiv] 전체 통과 ✅")


if __name__ == "__main__":
    main()
