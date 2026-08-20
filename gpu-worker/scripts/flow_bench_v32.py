# -*- coding: utf-8 -*-
"""RC4 Phase D: flow 엔진 비교 벤치 (실영상 파생 GT 3케이스).

엔진: dis_half(현행) / dis_full / farneback / raft_small / raft_large(torchvision)
케이스: pan(팬+상시자막) / transient(정지+구간카드) / moving(실모션+상시자막)
지표: 마스크 내 PSNR(전파 수용 화소만), coverage, 프레임당 시간.
산출: RC4_FLOW_ENGINE_BENCHMARK.csv

주의: SEA-RAFT(BSD-3)는 공식 가중치 배포 채널(GDrive) 접근성 문제로 이번 벤치는
torchvision RAFT(BSD-3, 같은 계열 아키텍처)로 상한 근사를 먼저 재고, SEA-RAFT
전용 통합은 결과가 DIS 대비 유의미할 때만 진행한다 (명세 D — 후보 비교 우선).
"""
import csv
import os
import subprocess
import sys
import tempfile
import time

import numpy as np
import cv2
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import restore_rc4 as R

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
W = H = 480
FPS = 30
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[-400:])


def download_master(tmp):
    fp = os.path.join(tmp, "master.mp4")
    r = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/"
                     f"bench-assets/benchmark_master.mp4",
                     headers={"apikey": SB_KEY,
                              "Authorization": f"Bearer {SB_KEY}"},
                     stream=True, timeout=1800)
    r.raise_for_status()
    with open(fp, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)
    return fp


def read_frames(path):
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    out = []
    while True:
        b = p.stdout.read(W * H * 3)
        if len(b) < W * H * 3:
            break
        out.append(np.frombuffer(b, np.uint8).reshape(H, W, 3).copy())
    p.wait()
    return out


def build_cases(tmp, master):
    cases = {}
    still = os.path.join(tmp, "still.png")
    sh(["ffmpeg", "-v", "error", "-ss", "60", "-i", master, "-frames:v", "1",
        "-vf", "scale=1600:-2", still, "-y"])
    a_c = os.path.join(tmp, "a_clean.mp4")
    sh(["ffmpeg", "-v", "error", "-loop", "1", "-i", still, "-t", "3",
        "-vf", f"crop={W}:{H}:t*90:60,fps={FPS}", "-an",
        "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", a_c, "-y"])
    a_i = os.path.join(tmp, "a_in.mp4")
    sh(["ffmpeg", "-v", "error", "-i", a_c, "-vf",
        f"drawtext=fontfile={FONT}:text='PAN SUBTITLE':fontsize=44:"
        f"fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-90",
        "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", a_i, "-y"])
    cases["pan"] = (a_c, a_i)
    for name, ss, flt in [
        ("transient",
         "30",
         f"drawtext=fontfile={FONT}:text='CARD':fontsize=52:fontcolor=0x101010:"
         f"box=1:boxcolor=white@0.85:boxborderw=22:x=(w-text_w)/2:"
         f"y=(h-text_h)/2:enable='between(t,1,2)'"),
        ("moving",
         "30",
         f"drawtext=fontfile={FONT}:text='STATIC MARK':fontsize=46:"
         f"fontcolor=white:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-90"),
    ]:
        c = os.path.join(tmp, f"{name}_clean.mp4")
        sh(["ffmpeg", "-v", "error", "-ss", ss, "-t", "3", "-i", master,
            "-vf", f"crop={W}:{H}:300:150,fps={FPS}", "-an",
            "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", c, "-y"])
        i = os.path.join(tmp, f"{name}_in.mp4")
        sh(["ffmpeg", "-v", "error", "-i", c, "-vf", flt,
            "-c:v", "libx264", "-crf", "12", "-preset", "veryfast", i, "-y"])
        cases[name] = (c, i)
    return cases


def masks_from_diff(cl, ip):
    ms = []
    for c, f in zip(cl, ip):
        d = np.abs(c.astype(np.int16) - f.astype(np.int16)).max(axis=2)
        m = (d > 18).astype(np.uint8) * 255
        m = cv2.dilate(m, np.ones((7, 7), np.uint8))
        ms.append(m if m.any() else None)
    return ms


# ---------------- 엔진 어댑터 ----------------
def make_engine(kind):
    if kind == "dis_half":
        eng = R._dis()
        return lambda a, b: R._flow_pair(a, b, eng, half=True)
    if kind == "dis_full":
        eng = R._dis()
        return lambda a, b: R._flow_pair(a, b, eng, half=False)
    if kind == "farneback":
        return lambda a, b: cv2.calcOpticalFlowFarneback(
            a, b, None, 0.5, 4, 21, 3, 5, 1.1, 0)
    if kind in ("raft_small", "raft_large"):
        import torch
        from torchvision.models import optical_flow as of
        if kind == "raft_small":
            model = of.raft_small(weights=of.Raft_Small_Weights.DEFAULT)
        else:
            model = of.raft_large(weights=of.Raft_Large_Weights.DEFAULT)
        model = model.eval()

        def raft(a, b):
            with torch.no_grad():
                t1 = torch.from_numpy(np.stack([a] * 3)).float()[None] / 127.5 - 1
                t2 = torch.from_numpy(np.stack([b] * 3)).float()[None] / 127.5 - 1
                fl = model(t1, t2)[-1][0].numpy()
            return np.moveaxis(fl, 0, -1).astype(np.float32)
        return raft
    raise ValueError(kind)


def bench_engine(kind, cl, ip, ms, tis, offsets=(2, 8, 20)):
    flow_fn = make_engine(kind)
    orig = R._flow_pair
    R._flow_pair = lambda a, b, engine=None, half=True: flow_fn(a, b)
    try:
        pin, pout, covs = [], [], []
        t0 = time.time()
        for ti in tis:
            m = ms[ti]
            if m is None or not m.any():
                continue
            filled, wgt, hole = R.propagate_frame(ip, ms, ti, offsets=offsets,
                                                  gray_cache={})
            need = m > 127
            valid = need & (wgt >= R.MIN_VALID_W)
            cov = float(valid.sum()) / max(1, int(need.sum()))
            covs.append(cov)
            if valid.any():
                d = (filled - cl[ti].astype(np.float32))[valid]
                mse = float((d ** 2).mean())
                pout.append(10 * np.log10(255 * 255 / max(1e-6, mse)))
                d0 = (ip[ti].astype(np.float32) - cl[ti].astype(np.float32))[valid]
                pin.append(10 * np.log10(255 * 255
                                         / max(1e-6, float((d0 ** 2).mean()))))
        dt = (time.time() - t0) / max(1, len(tis))
        return (round(float(np.mean(pin)) if pin else 0, 2),
                round(float(np.mean(pout)) if pout else 0, 2),
                round(float(np.mean(covs)) if covs else 0, 3),
                round(dt, 2))
    finally:
        R._flow_pair = orig


def main():
    tmp = tempfile.mkdtemp(prefix="flowbench-")
    master = download_master(tmp)
    cases = build_cases(tmp, master)
    engines = ["dis_half", "dis_full", "farneback"]
    try:
        import torch  # noqa
        engines += ["raft_small", "raft_large"]
    except ImportError:
        print("[flowbench] torch 없음 — RAFT 생략")
    rows = []
    for cname, (cp, ipp) in cases.items():
        cl = read_frames(cp)
        ip = read_frames(ipp)
        ms = masks_from_diff(cl, ip)
        masked = [i for i, m in enumerate(ms) if m is not None and m.any()]
        tis = masked[:: max(1, len(masked) // 8)][:8]
        for e in engines:
            try:
                pi, po, cov, sec = bench_engine(e, cl, ip, ms, tis)
                rows.append({"engine": e, "case": cname, "psnr_in": pi,
                             "psnr_fill": po, "coverage": cov,
                             "sec_per_frame": sec})
                print(f"[FLOWBENCH] {e} {cname} fill={po}dB cov={cov} "
                      f"t={sec}s/fr")
            except Exception as ex:
                rows.append({"engine": e, "case": cname, "psnr_in": "",
                             "psnr_fill": "ERR", "coverage": "",
                             "sec_per_frame": str(ex)[:60]})
                print(f"[FLOWBENCH] {e} {cname} ERROR {str(ex)[:120]}")
    with open("RC4_FLOW_ENGINE_BENCHMARK.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("[flowbench] done", len(rows))


if __name__ == "__main__":
    main()
