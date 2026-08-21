# -*- coding: utf-8 -*-
"""G3 토너먼트 육안 비교 자료 생성기.

ROI 팩 폴더(bench-assets/tournament/{roi}/)의 input + cand_* (+골든 GT)를
내려받아:
  1) 프레임 그리드 PNG (열=시점, 행=input/후보들/GT) → {UID}/rc4_review/
  2) 나란히 재생 mp4 (input|후보들 hstack, 각 480px 폭) → {UID}/rc4_review/
을 업로드한다. 사이트 로그인 세션의 signed URL로 대표가 바로 열람.

CONTACT_SPEC (JSON):
  [{"roi":"g26","golden":"g26","times":[2,6,11,16]},
   {"roi":"uat01_t154","times":[1,2.5,4]}]
'golden'이 있으면 bench-assets/golden/{golden}_clean.mp4 의 같은 창을 GT로 포함
(meta.json의 f0/fps로 창 정렬).
"""
import json
import os
import subprocess
import sys
import tempfile

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
UID = "4117e902-3396-4b14-aea0-957b326ab563"
SRC = "bench-assets/tournament"


def hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def dl(path, dst, bucket="videos-clips"):
    with requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                      headers=hdr(), stream=True, timeout=1800) as r:
        if not r.ok:
            return None
        with open(dst, "wb") as f:
            for c in r.iter_content(1 << 20):
                f.write(c)
    return os.path.getsize(dst)


def up(src, path, ctype):
    with open(src, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                          headers=hdr({"Content-Type": ctype,
                                       "x-upsert": "true"}), data=f.read(),
                          timeout=1800)
    r.raise_for_status()


def main():
    spec = json.loads(os.environ.get("CONTACT_SPEC", "[]"))
    if not spec:
        print("[CONTACT] empty CONTACT_SPEC")
        sys.exit(1)
    fail = 0
    for item in spec:
        roi = item["roi"]
        tmpd = tempfile.mkdtemp(prefix="ct-")
        metap = os.path.join(tmpd, "meta.json")
        if not dl(f"{SRC}/{roi}/meta.json", metap):
            print(f"[CONTACT] meta 없음 roi={roi}")
            fail += 1
            continue
        meta = json.loads(open(metap).read())
        fps, f0, f1 = meta["fps"], meta["f0"], meta["f1"]
        vids = []  # (label, path)
        inp = os.path.join(tmpd, "input.mp4")
        dl(f"{SRC}/{roi}/input.mp4", inp)
        vids.append(("input", inp))
        for cand in ("cand_A", "cand_B", "cand_C", "cand_D", "cand_E",
                     "cand_E_w0", "cand_C_w0", "cand_F", "cand_G", "cand_H"):
            p = os.path.join(tmpd, cand + ".mp4")
            if dl(f"{SRC}/{roi}/{cand}.mp4", p):
                vids.append((cand, p))
        if item.get("golden"):
            g = item["golden"]
            gclean = os.path.join(tmpd, "gt_full.mp4")
            if dl(f"bench-assets/golden/{g}_clean.mp4", gclean):
                gt = os.path.join(tmpd, "gt.mp4")
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", gclean, "-vf",
                     f"select=between(n\\,{f0}\\,{f1 - 1}),setpts=N/FRAME_RATE/TB",
                     "-r", str(fps), "-frames:v", str(f1 - f0), "-an",
                     "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p",
                     gt, "-y"], check=True)
                vids.append(("GT", gt))
        # 1) 그리드 PNG: 시점별 각 영상 프레임 hstack + 라벨
        for t in item.get("times", []):
            frames = []
            for label, p in vids:
                fp = os.path.join(tmpd, f"f_{label}_{t}.png")
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", f"{float(t):.3f}",
                     "-i", p, "-frames:v", "1",
                     "-vf", ("scale=480:-2,"
                             f"drawtext=text='{label}':x=8:y=8:fontsize=22:"
                             "fontcolor=yellow:box=1:boxcolor=black@0.5"),
                     fp, "-y"], check=True)
                if os.path.exists(fp):
                    frames.append(fp)
            if len(frames) < 2:
                continue
            grid = os.path.join(tmpd, f"grid_{t}.png")
            ins = []
            for fp in frames:
                ins += ["-i", fp]
            subprocess.run(
                ["ffmpeg", "-v", "error", *ins, "-filter_complex",
                 "".join(f"[{i}:v]" for i in range(len(frames)))
                 + f"hstack={len(frames)}", grid, "-y"], check=True)
            key = f"{UID}/rc4_review/tourn_{roi}_t{t}.png"
            up(grid, key, "image/png")
            print(f"[CONTACT] {key} panels={len(frames)}")
        # 2) 나란히 재생 mp4
        if len(vids) >= 2:
            ins = []
            for _l, p in vids:
                ins += ["-i", p]
            fc = "".join(
                f"[{i}:v]scale=480:-2,drawtext=text='{vids[i][0]}':x=8:y=8:"
                f"fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.5[v{i}];"
                for i in range(len(vids)))
            fc += "".join(f"[v{i}]" for i in range(len(vids)))
            fc += f"hstack={len(vids)}[out]"
            sxs = os.path.join(tmpd, "sxs.mp4")
            subprocess.run(
                ["ffmpeg", "-v", "error", *ins, "-filter_complex", fc,
                 "-map", "[out]", "-c:v", "libx264", "-crf", "18",
                 "-pix_fmt", "yuv420p", sxs, "-y"], check=True)
            key = f"{UID}/rc4_review/tourn_{roi}_sxs.mp4"
            up(sxs, key, "video/mp4")
            print(f"[CONTACT] {key} panels={len(vids)}")
    print(f"[CONTACT] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
