# -*- coding: utf-8 -*-
"""후보 D/F 하이브리드 오케스트레이터 (후속 명세 Phase 6).

구조 (production H.2/H.3와 동일한 순서를 파이프라인 안에서 그대로 사용):
  segment_v32(residual_export=1) —
    카드 un-blend → flow 실화소 전파 → residual hole 재료(crop+context)만
    npz로 내보내고 자리채움 → Preserver
  → 본 스크립트가 각 residual 재료를 SVOR(F)/순정 VACE(D)로 복원
  → hole alpha(feather 2px)로 seg 출력에 되붙여 cand_D/cand_F 생성

HYBRID_SPEC (JSON): [{"pid":"...","roi":"g26","t0":0,"t1":999,
                      "cands":["D","F"]}]
같은 pid 항목은 scan/segment 공유. 산출:
  bench-assets/tournament/{roi}/cand_D.mp4 · cand_F.mp4 (+ hybrid_meta.json)
기록: real_pixel_coverage(flow_cover), residual_hole_ratio, resid pack 수,
      SVOR/VACE 호출 시간·VRAM (RAW 로그).
"""
import io
import json
import hashlib
import os
import subprocess
import sys
import tempfile

import numpy as np
import cv2
import requests
import modal

APP = "inbilab-wm-gpu-v32-speed-staging"
SVOR_APP = "inbilab-wm-svor-staging"
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
DEST = "bench-assets/tournament"
LORA = {"D": "none", "F": "stage12"}


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


def up(src, path, ctype="video/mp4"):
    with open(src, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                          headers=hdr({"Content-Type": ctype,
                                       "x-upsert": "true"}), data=f.read(),
                          timeout=1800)
    r.raise_for_status()


def ls_prefix(prefix):
    r = requests.post(f"{SB_URL}/storage/v1/object/list/videos-clips",
                      headers=hdr({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": prefix, "limit": 1000}),
                      timeout=120)
    r.raise_for_status()
    return [x["name"] for x in r.json()]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 22), b""):
            h.update(c)
    return h.hexdigest()


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


def write_mp4(frames, path, fps, crf=12):
    h, w = frames[0].shape[:2]
    pipe = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
         "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
         "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
         path, "-y"], stdin=subprocess.PIPE)
    for f in frames:
        pipe.stdin.write(f.tobytes())
    pipe.stdin.close()
    pipe.wait()
    if pipe.returncode != 0:
        raise RuntimeError("encode 실패 " + path)


def main():
    spec = json.loads(os.environ.get("HYBRID_SPEC", "[]"))
    if not spec:
        print("[HYB] empty HYBRID_SPEC")
        sys.exit(1)
    scan_fn = modal.Function.from_name(APP, "scan_v32_cpu")
    seg_fn = modal.Function.from_name(APP, "segment_v32_gpu")
    svor_fn = modal.Function.from_name(SVOR_APP, "svor_h100")
    by_pid = {}
    for it in spec:
        by_pid.setdefault(it["pid"], []).append(it)
    fail = 0
    for pid, items in by_pid.items():
        cands = sorted({c for it in items for c in it.get("cands", ["D", "F"])})
        print(f"[HYB] scan pid={pid} cands={cands}")
        scan = scan_fn.remote({"input": {"project_id": pid,
                                         "phase": "scan_v32", "seg_k": 10}})
        if scan.get("note") == "no_target" or "error" in scan:
            print(f"[HYB] scan 실패: {json.dumps(scan)[:300]}")
            fail += len(items)
            continue
        segments = scan["segments"]
        tmpd = tempfile.mkdtemp(prefix="hyb-")
        planp = os.path.join(tmpd, "plan.json")
        dl("videos-clips", f"wmtmp-v32/{pid}/plan.json", planp)
        plan = json.loads(open(planp).read())
        fps, W, H, N = plan["fps"], plan["W"], plan["H"], plan["N"]
        need = set()
        for it in items:
            it["f0"] = max(0, int(round(it["t0"] * fps)))
            it["f1"] = min(N, int(round(it["t1"] * fps)))
            for k, (a, b) in enumerate(segments):
                if it["f0"] < b and it["f1"] > a:
                    need.add(k)
        need = sorted(need)
        print(f"[HYB] parts={need} fps={fps} N={N}")
        # 기존 resid 잔재 제거 후 실행 (재실행 안전)
        calls = [(k, seg_fn.spawn({"input": {"project_id": pid,
                                             "phase": "segment_v32", "part": k,
                                             "mask_export": 1,
                                             "residual_export": 1}}))
                 for k in need]
        segstats = {}
        for k, c in calls:
            r = c.get(timeout=1800)
            segstats[k] = r.get("counters", {})
            print(f"[HYB] seg part={k} err={r.get('error')} "
                  f"counters={json.dumps(segstats[k])[:300]}")
            if "error" in r:
                fail += 1
        # 자산 회수
        segvid = {}
        for k in need:
            sv = os.path.join(tmpd, f"seg_{k}.mp4")
            if dl("videos-clips", f"wmtmp-v32/{pid}/seg_{k}.mp4", sv):
                segvid[k] = read_frames(sv)
        resids = [n for n in ls_prefix(f"wmtmp-v32/{pid}")
                  if n.startswith("resid_") and n.endswith(".npz")
                  and int(n.split("_")[1]) in need]   # 이전 실행 잔재 배제
        print(f"[HYB] resid packs={len(resids)}")
        # 후보별 seg 프레임 사본
        comp = {cd: {k: [f.copy() for f in v] for k, v in segvid.items()}
                for cd in cands}
        pack_meta = []
        for nm in sorted(resids):
            rp = os.path.join(tmpd, nm)
            if not dl("videos-clips", f"wmtmp-v32/{pid}/{nm}", rp):
                continue
            z = np.load(rp)
            meta = json.loads(bytes(z["meta"]).decode())
            pk, rg = meta["part"], meta["reg"]
            cs, ce, E0 = meta["cs"], meta["ce"], meta["E0"]
            x0c, y0c, x1c, y1c = meta["crop"]
            rx, ry = meta["reg_xy"]
            hh, ww = meta["shape"]
            n = ce - cs + 1
            frames, holes = [], []
            for j in range(n):
                frames.append(cv2.imdecode(z[f"f{j}"], cv2.IMREAD_COLOR))
                holes.append((np.unpackbits(z[f"m{j}"])[:hh * ww]
                              .reshape(hh, ww) * 255).astype(np.uint8))
            inp = os.path.join(tmpd, nm + ".in.mp4")
            mkp = os.path.join(tmpd, nm + ".mask.mp4")
            write_mp4(frames, inp, fps)
            write_mp4([cv2.cvtColor(h2, cv2.COLOR_GRAY2BGR) for h2 in holes],
                      mkp, fps)
            key_in = f"wmtmp-v32/{pid}/{nm}.in.mp4"
            key_mk = f"wmtmp-v32/{pid}/{nm}.mask.mp4"
            up(inp, key_in)
            up(mkp, key_mk)
            rec = {"pack": nm, "part": pk, "reg": rg, "cs": cs, "ce": ce,
                   "E0": E0, "crop": [x0c, y0c, x1c, y1c],
                   "reg_xy": [rx, ry], "n": n,
                   "hole_px": int(sum(int((h2 > 0).sum()) for h2 in holes))}
            for cd in cands:
                out_key = f"wmtmp-v32/{pid}/{nm}.out_{cd}.mp4"
                ev = {"op": "roi", "video": key_in, "mask": key_mk,
                      "out": out_key, "lora": LORA[cd], "frames": n,
                      "dilation": 2, "steps": 20,
                      "max_area": min(720 * 1280, hh * ww)}
                try:
                    res = svor_fn.remote(ev)
                except Exception as e:  # noqa: BLE001
                    res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                print(f"[HYB] pack={nm} cand={cd} -> "
                      f"{json.dumps({x: res.get(x) for x in ('ok', 'run_s', 'vram_gb', 'error')})}")
                if not res.get("ok"):
                    fail += 1
                    continue
                op = os.path.join(tmpd, nm + f".out_{cd}.mp4")
                dl("videos-clips", out_key, op)
                gen = read_frames(op)
                # 되붙임: hole alpha feather 2px (rc4 hole 합성과 동일)
                for j in range(min(n, len(gen))):
                    g = E0 + cs + j
                    for k2, (a2, b2) in enumerate(segments):
                        if k2 in comp[cd] and a2 <= g < b2:
                            fr = comp[cd][k2][g - a2]
                            gy0, gy1 = ry + y0c, ry + y1c
                            gx0, gx1 = rx + x0c, rx + x1c
                            gimg = gen[j]
                            if gimg.shape[:2] != (hh, ww):
                                gimg = cv2.resize(gimg, (ww, hh),
                                                  interpolation=cv2.INTER_LANCZOS4)
                            al = cv2.GaussianBlur(holes[j], (0, 0), 2) \
                                .astype(np.float32)[..., None] / 255.0
                            sub = fr[gy0:gy1, gx0:gx1].astype(np.float32)
                            fr[gy0:gy1, gx0:gx1] = np.clip(
                                sub * (1 - al) + gimg.astype(np.float32) * al,
                                0, 255).astype(np.uint8)
                            break
            pack_meta.append(rec)
        # ROI 창 잘라 업로드
        for it in items:
            f0, f1, roi = it["f0"], it["f1"], it["roi"]
            spans = [(k, a, b) for k, (a, b) in enumerate(segments)
                     if f0 < b and f1 > a]
            for cd in it.get("cands", ["D", "F"]):
                seq = []
                okall = all(k in comp[cd] for k, _a, _b in spans)
                if not okall:
                    print(f"[HYB] roi={roi} cand={cd} seg 누락")
                    fail += 1
                    continue
                for k, a, b in spans:
                    lo = max(f0, a) - a
                    hi = min(f1, b) - a
                    seq.extend(comp[cd][k][lo:hi])
                outp = os.path.join(tmpd, f"{roi}_cand_{cd}.mp4")
                write_mp4(seq, outp, fps)
                up(outp, f"{DEST}/{roi}/cand_{cd}.mp4")
                print(f"[HYB] roi={roi} cand_{cd} frames={len(seq)} "
                      f"sha256={sha(outp)}")
            # hybrid 통계
            covs = [segstats.get(k, {}) for k, _a, _b in spans]
            used = sum(c.get("flow_used", 0) for c in covs)
            csum = sum(c.get("flow_cover_pct_sum", 0) for c in covs)
            hmeta = {"roi": roi,
                     "flow_used_chunks": used,
                     "flow_bypass_chunks": sum(c.get("flow_bypass", 0)
                                               for c in covs),
                     "real_pixel_coverage_avg_pct":
                         round(csum / used, 1) if used else None,
                     "resid_packs": len(pack_meta),
                     "resid_hole_px": sum(p["hole_px"] for p in pack_meta)}
            mf = os.path.join(tmpd, f"{roi}_hybrid_meta.json")
            open(mf, "w").write(json.dumps(hmeta))
            up(mf, f"{DEST}/{roi}/hybrid_meta.json", "application/json")
            print(f"[HYB] roi={roi} meta={json.dumps(hmeta)}")
    print(f"[HYB] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
