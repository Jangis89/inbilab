# -*- coding: utf-8 -*-
"""Vmake 결과물을 토너먼트 비교 체계에 편입 (후속 명세 §Vmake 비교 포함 지시).

bench-assets/vmake/{src}에 보존된 Vmake 전체 결과 영상에서 각 ROI 창
[t0,t1)을 프레임 정확하게 잘라 bench-assets/tournament/{roi}/cand_V.mp4로
업로드한다. 이후 tournmetrics/tourncontact가 cand_V를 다른 후보와 동일
조건으로 취급할 수 있다. (비교용 참고 후보 — production 후보 아님.)

VMAKE_SPEC (JSON): [{"roi","src","t0","t1","fps":30}]
"""
import json
import hashlib
import os
import subprocess
import sys
import tempfile

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]


def hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def dl(path, dst):
    for att in range(3):
        try:
            with requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                              headers=hdr(), stream=True, timeout=1800) as r:
                if r.status_code in (400, 404):
                    return None
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for c in r.iter_content(1 << 20):
                        f.write(c)
            return os.path.getsize(dst)
        except Exception as e:  # noqa: BLE001
            if att == 2:
                print(f"[VMK] dl 실패 {path}: {type(e).__name__}: {e}")
                return None
            import time as _t
            _t.sleep(5 * (att + 1))


def up(src, path):
    for att in range(3):
        try:
            with open(src, "rb") as f:
                r = requests.post(
                    f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                    headers=hdr({"Content-Type": "video/mp4",
                                 "x-upsert": "true"}),
                    data=f.read(), timeout=1800)
            r.raise_for_status()
            return
        except Exception as e:  # noqa: BLE001
            if att == 2:
                raise
            print(f"[VMK] up 재시도 {path}: {type(e).__name__}")
            import time as _t
            _t.sleep(5 * (att + 1))


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 22), b""):
            h.update(c)
    return h.hexdigest()


def main():
    spec = json.loads(os.environ.get("VMAKE_SPEC", "[]"))
    if not spec:
        print("[VMK] empty VMAKE_SPEC")
        sys.exit(1)
    tmpd = tempfile.mkdtemp(prefix="vmk-")
    srcs = {}
    fail = 0
    for it in spec:
        src = it["src"]
        if src not in srcs:
            sp = os.path.join(tmpd, os.path.basename(src))
            if not dl(f"bench-assets/{src}", sp):
                print(f"[VMK] 소스 없음: {src}")
                fail += 1
                srcs[src] = None
                continue
            srcs[src] = sp
        sp = srcs[src]
        if not sp:
            fail += 1
            continue
        fps = float(it.get("fps", 30))
        f0 = max(0, int(round(it["t0"] * fps)))
        f1 = int(round(it["t1"] * fps))
        outp = os.path.join(tmpd, f"{it['roi']}_cand_V.mp4")
        cmd = ["ffmpeg", "-v", "error", "-i", sp,
               "-vf", f"trim=start_frame={f0}:end_frame={f1},"
                      f"setpts=PTS-STARTPTS",
               "-an", "-c:v", "libx264", "-crf", "12", "-preset", "medium",
               "-pix_fmt", "yuv420p", outp, "-y"]
        rc = subprocess.run(cmd).returncode
        if rc != 0 or not os.path.exists(outp):
            print(f"[VMK] {it['roi']} 인코딩 실패 rc={rc}")
            fail += 1
            continue
        key = f"bench-assets/tournament/{it['roi']}/cand_V.mp4"
        up(outp, key)
        print(f"[VMK] {it['roi']} frames={f1 - f0} sha256={sha(outp)} -> {key}",
              flush=True)
    print(f"[VMK] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
