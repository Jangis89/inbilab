# -*- coding: utf-8 -*-
"""G3 토너먼트 ROI 팩 추출기 (동일 입력·동일 effect mask 동결 — 명세 G3.3).

흐름 (소스 pid당):
  1) scan_v32 (CPU) — 운영과 동일한 감지·plan
  2) ROI 초구간을 덮는 segment part만 mask_export=1로 실행 (동일 파이프라인이
     실제로 손대는 allowed mask를 프레임 단위 동결)
  3) roimask_{part}.npz + work/source에서 ROI 창을 잘라
     bench-assets/tournament/{roi_id}/{input,mask}.mp4 + meta.json 업로드

TOURN_SPEC (JSON):
  [{"pid":"...","roi":"g26","t0":0,"t1":20},
   {"pid":"...","roi":"uat01_t154","t0":152,"t1":157}, ...]
같은 pid 항목은 scan/segment를 공유한다.

candidate A(flow-only) 출력은 같은 실행의 seg_{part}.mp4가 이미 그 결과이므로
여기서 같은 창으로 잘라 {roi_id}/cand_A.mp4 로 함께 보존한다.
"""
import io
import json
import hashlib
import os
import subprocess
import sys
import tempfile

import numpy as np
import requests
import modal

APP = "inbilab-wm-gpu-v32-speed-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
DEST_PREFIX = "bench-assets/tournament"


def hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def dl(bucket, path, dst):
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


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 22), b""):
            h.update(c)
    return h.hexdigest()


def cut(src, f0, f1, fps, out, crf=12):
    subprocess.run(["ffmpeg", "-v", "error", "-i", src,
                    "-vf", f"select=between(n\\,{f0}\\,{f1 - 1}),setpts=N/FRAME_RATE/TB",
                    "-r", str(fps), "-frames:v", str(f1 - f0), "-an",
                    "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                    "-pix_fmt", "yuv420p", out, "-y"], check=True)


def main():
    spec = json.loads(os.environ.get("TOURN_SPEC", "[]"))
    if not spec:
        print("[TOURN] empty TOURN_SPEC")
        sys.exit(1)
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    by_pid = {}
    for item in spec:
        by_pid.setdefault(item["pid"], []).append(item)
    fail = 0
    for pid, items in by_pid.items():
        print(f"[TOURN] scan pid={pid}")
        scan = scan_fn.remote({"input": {"project_id": pid,
                                         "phase": "scan_v32", "seg_k": 10}})
        if scan.get("note") == "no_target" or "error" in scan:
            print(f"[TOURN] scan failed: {json.dumps(scan)[:400]}")
            fail += len(items)
            continue
        segments = scan["segments"]
        # plan에서 fps/W/H 회수
        tmpd = tempfile.mkdtemp(prefix="tourn-")
        planp = os.path.join(tmpd, "plan.json")
        if not dl("wmtmp-v32", f"{pid}/plan.json", planp):
            print(f"[TOURN] plan.json missing pid={pid}")
            fail += len(items)
            continue
        plan = json.loads(open(planp).read())
        fps, W, H, N = plan["fps"], plan["W"], plan["H"], plan["N"]
        # 필요한 part 집합
        need = {}
        for it in items:
            f0 = max(0, int(round(it["t0"] * fps)))
            f1 = min(N, int(round(it["t1"] * fps)))
            it["f0"], it["f1"] = f0, f1
            for k, (a, b) in enumerate(segments):
                if f0 < b and f1 > a:
                    need.setdefault(k, []).append(it["roi"])
        print(f"[TOURN] pid={pid} parts={sorted(need)} fps={fps} N={N}")
        calls = [(k, seg_fn.spawn({"input": {"project_id": pid,
                                             "phase": "segment_v32", "part": k,
                                             "mask_export": 1}}))
                 for k in sorted(need)]
        seg_res = {}
        for k, c in calls:
            r = c.get(timeout=1800)
            seg_res[k] = r
            print(f"[TOURN] seg part={k} -> "
                  f"{json.dumps({x: r.get(x) for x in ('part', 'frames', 'error')})}")
            if "error" in r:
                fail += 1
        # 마스크 npz + seg 출력 회수
        masks = {}     # global frame -> packbits row
        segvid = {}
        for k in sorted(need):
            mz = os.path.join(tmpd, f"roimask_{k}.npz")
            if dl("wmtmp-v32", f"{pid}/roimask_{k}.npz", mz):
                z = np.load(mz)
                F0k, F1k, Hk, Wk = [int(x) for x in z["meta"]]
                for j in range(F1k - F0k):
                    masks[F0k + j] = z[f"m{j}"]
            sv = os.path.join(tmpd, f"seg_{k}.mp4")
            if dl("wmtmp-v32", f"{pid}/seg_{k}.mp4", sv):
                segvid[k] = sv
        # work(CFR) 우선, 없으면 원본 소스
        workp = os.path.join(tmpd, "work.mp4")
        if not dl("wmtmp-v32", f"{pid}/work.mp4", workp):
            proj = requests.get(
                f"{SB_URL}/rest/v1/sc_projects?id=eq.{pid}&select=source_path",
                headers=hdr()).json()
            spath = proj[0]["source_path"]
            b, p = spath.split("/", 1) if not spath.startswith("videos-source") \
                else ("videos-source", spath.split("/", 1)[1])
            if "/" in spath and spath.split("/")[0] in ("videos-source",
                                                        "videos-clips"):
                b, p = spath.split("/", 1)
            if not dl(b, p, workp):
                print(f"[TOURN] source download 실패 pid={pid} {spath}")
                fail += len(items)
                continue
        for it in items:
            roi, f0, f1 = it["roi"], it["f0"], it["f1"]
            outdir = os.path.join(tmpd, roi)
            os.makedirs(outdir, exist_ok=True)
            inp = os.path.join(outdir, "input.mp4")
            cut(workp, f0, f1, fps, inp)
            # 마스크 → 무손실 회색 mp4 (libx264rgb 대신 crf0 gray)
            mk = os.path.join(outdir, "mask.mp4")
            pipe = subprocess.Popen(
                ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
                 "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
                 "-c:v", "libx264", "-qp", "0", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", mk, "-y"], stdin=subprocess.PIPE)
            miss = 0
            for gi in range(f0, f1):
                if gi in masks:
                    row = np.unpackbits(masks[gi])[:H * W]
                    m = (row.reshape(H, W) * 255).astype(np.uint8)
                else:
                    m = np.zeros((H, W), np.uint8)
                    miss += 1
                pipe.stdin.write(m.tobytes())
            pipe.stdin.close()
            pipe.wait()
            # candidate A = 현 파이프라인 seg 출력에서 같은 창
            candA = os.path.join(outdir, "cand_A.mp4")
            segsrcs = [segvid[k] for k, (a, b) in enumerate(segments)
                       if k in segvid and f0 < b and f1 > a]
            candA_ok = False
            if segsrcs:
                # part별 seg는 [F0,F1) 프레임만 담고 있음 — 창이 한 part에
                # 들어가면 그 파일에서 상대 창으로 잘라냄 (여러 part에 걸치면
                # concat 필요 — v1은 단일 part 창만 지원, 걸침은 기록만)
                spans = [(k, a, b) for k, (a, b) in enumerate(segments)
                         if k in segvid and f0 < b and f1 > a]
                if len(spans) == 1:
                    k, a, b = spans[0]
                    cut(segvid[k], f0 - a, f1 - a, fps, candA)
                    candA_ok = True
            meta = {"pid": pid, "roi": roi, "f0": f0, "f1": f1, "fps": fps,
                    "W": W, "H": H, "mask_missing_frames": miss,
                    "candA_single_part": candA_ok,
                    "input_sha256": sha(inp), "mask_sha256": sha(mk)}
            if candA_ok:
                meta["cand_A_sha256"] = sha(candA)
            mfile = os.path.join(outdir, "meta.json")
            open(mfile, "w").write(json.dumps(meta, indent=1))
            up(inp, f"{DEST_PREFIX}/{roi}/input.mp4", "video/mp4")
            up(mk, f"{DEST_PREFIX}/{roi}/mask.mp4", "video/mp4")
            if candA_ok:
                up(candA, f"{DEST_PREFIX}/{roi}/cand_A.mp4", "video/mp4")
            up(mfile, f"{DEST_PREFIX}/{roi}/meta.json", "application/json")
            print(f"[TOURN] roi={roi} frames={f1 - f0} miss={miss} "
                  f"candA={candA_ok} meta={json.dumps(meta)}")
            if miss:
                fail += 1
    print(f"[TOURN] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
