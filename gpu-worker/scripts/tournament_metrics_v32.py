# -*- coding: utf-8 -*-
"""G3.5 토너먼트 객관 지표 — ROI 팩의 후보 출력을 GT와 대조해 CSV 생성.

TOURN_MET_SPEC (JSON):
  [{"roi":"g26","golden":"g26","cands":["cand_A","cand_C_w0","cand_E_w0"]}]

생성 지표 (후보별):
  frames, psnr_in(마스크 안), psnr_out(마스크 밖), ssim_in,
  sharp_ratio(마스크 안 라플라시안 P50 / GT), flicker(시간축 차분, GT 대비 비율),
  out_maxdiff(합성 후 마스크 밖 최대 차 — Preserver 검증)
생성형 후보(cand_A 외)는 VAE 왕복으로 전체 화소가 변하므로, 실사용과 동일하게
'마스크 밖=원본, 안=후보' Preserver 합성 후 측정한다 (feather 2px).
결과: stdout 표 + TOURNAMENT_METRICS.csv (Actions artifact 용).
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np
import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
SRC = "bench-assets/tournament"


def hdr():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}


def dl(path, dst, bucket="videos-clips"):
    with requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                      headers=hdr(), stream=True, timeout=1800) as r:
        if not r.ok:
            return None
        with open(dst, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)
    return os.path.getsize(dst)


def read_frames(path, nmax=100000):
    cap = cv2.VideoCapture(path)
    out = []
    while len(out) < nmax:
        ok, fr = cap.read()
        if not ok:
            break
        out.append(fr)
    cap.release()
    return out


def psnr(a, b, sel=None):
    d = (a.astype(np.float32) - b.astype(np.float32)) ** 2
    if sel is not None:
        if not sel.any():
            return float("nan")
        m = float(d[sel].mean())
    else:
        m = float(d.mean())
    if m <= 1e-9:
        return 99.0
    return float(10 * np.log10(255.0 ** 2 / m))


def ssim_gray(a, b, sel=None):
    a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    va = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a * mu_a
    vb = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b * mu_b
    vab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_a * mu_b
    s = ((2 * mu_a * mu_b + C1) * (2 * vab + C2)) / (
        (mu_a ** 2 + mu_b ** 2 + C1) * (va + vb + C2) + 1e-9)
    if sel is not None:
        return float(s[sel].mean()) if sel.any() else float("nan")
    return float(s.mean())


def lap_p50(img, sel):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    L = np.abs(cv2.Laplacian(g, cv2.CV_32F))
    return float(np.percentile(L[sel], 50)) if sel.any() else float("nan")


def main():
    spec = json.loads(os.environ.get("TOURN_MET_SPEC", "[]"))
    if not spec:
        print("[MET] empty spec")
        sys.exit(1)
    rows = []
    fail = 0
    for item in spec:
        roi = item["roi"]
        tmpd = tempfile.mkdtemp(prefix="met-")
        metap = os.path.join(tmpd, "meta.json")
        if not dl(f"{SRC}/{roi}/meta.json", metap):
            print(f"[MET] meta 없음 roi={roi}")
            fail += 1
            continue
        meta = json.loads(open(metap).read())
        f0, f1, fps = meta["f0"], meta["f1"], meta["fps"]
        inp = os.path.join(tmpd, "input.mp4")
        msk = os.path.join(tmpd, "mask.mp4")
        dl(f"{SRC}/{roi}/input.mp4", inp)
        dl(f"{SRC}/{roi}/mask.mp4", msk)
        IN = read_frames(inp)
        MK = [(cv2.cvtColor(m, cv2.COLOR_BGR2GRAY) > 127) for m in
              read_frames(msk)]
        GT = None
        if item.get("golden"):
            gfull = os.path.join(tmpd, "gt_full.mp4")
            if dl(f"bench-assets/golden/{item['golden']}_clean.mp4", gfull):
                gcut = os.path.join(tmpd, "gt.mp4")
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", gfull, "-vf",
                     f"select=between(n\\,{f0}\\,{f1 - 1}),setpts=N/FRAME_RATE/TB",
                     "-r", str(fps), "-frames:v", str(f1 - f0), "-an",
                     "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
                     gcut, "-y"], check=True)
                GT = read_frames(gcut)
        for cand in item.get("cands", []):
            cp = os.path.join(tmpd, cand + ".mp4")
            if not dl(f"{SRC}/{roi}/{cand}.mp4", cp):
                print(f"[MET] {roi}/{cand} 없음 — 건너뜀")
                continue
            CD = read_frames(cp)
            n = min(len(IN), len(MK), len(CD), len(GT) if GT else 10 ** 9)
            if n == 0:
                continue
            comp = cand != "cand_A"    # 생성형은 Preserver 합성 후 측정
            pin, pout, sin_, shr, flk, omx = [], [], [], [], [], []
            prev_c = prev_g = None
            for i in range(n):
                a = IN[i]
                c = CD[i]
                if c.shape != a.shape:
                    c = cv2.resize(c, (a.shape[1], a.shape[0]),
                                   interpolation=cv2.INTER_LANCZOS4)
                m = MK[i]
                if comp:
                    mm = (m.astype(np.uint8) * 255)
                    al = cv2.GaussianBlur(mm, (0, 0), 2).astype(
                        np.float32)[..., None] / 255.0
                    c = np.clip(a.astype(np.float32) * (1 - al)
                                + c.astype(np.float32) * al, 0,
                                255).astype(np.uint8)
                ref = GT[i] if GT else None
                outm = ~m
                if ref is not None:
                    pin.append(psnr(c, ref, m))
                    pout.append(psnr(c, ref, outm))
                    sin_.append(ssim_gray(c, ref, m))
                    if m.any():
                        sc, sg = lap_p50(c, m), lap_p50(ref, m)
                        if sg > 1e-3:
                            shr.append(sc / sg)
                omx.append(float(np.abs(
                    c.astype(np.int16) - a.astype(np.int16))[outm].max())
                    if outm.any() else 0.0)
                if prev_c is not None and m.any():
                    dc = float(np.abs(c.astype(np.float32)
                                      - prev_c.astype(np.float32))[m].mean())
                    if ref is not None and prev_g is not None:
                        dg = float(np.abs(ref.astype(np.float32)
                                          - prev_g.astype(np.float32))[m].mean())
                        flk.append(dc / max(dg, 0.5))
                    else:
                        flk.append(dc)
                prev_c, prev_g = c, (GT[i] if GT else None)
            row = {"roi": roi, "cand": cand, "frames": n,
                   "psnr_in": round(float(np.nanmean(pin)), 2) if pin else "",
                   "psnr_out": round(float(np.nanmean(pout)), 2) if pout else "",
                   "ssim_in": round(float(np.nanmean(sin_)), 4) if sin_ else "",
                   "sharp_ratio": round(float(np.nanmean(shr)), 3) if shr else "",
                   "flicker_ratio": round(float(np.nanmean(flk)), 3) if flk else "",
                   "out_maxdiff": round(float(np.max(omx)), 1) if omx else ""}
            rows.append(row)
            print("[MET] " + json.dumps(row, ensure_ascii=False))
    if rows:
        with open("TOURNAMENT_METRICS.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"[MET] done rows={len(rows)} fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
