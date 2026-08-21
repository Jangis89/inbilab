# -*- coding: utf-8 -*-
"""후보 D/F 하이브리드 오케스트레이터 v2 (후속 명세 Phase 6).

v2: 합성을 스트리밍으로 재설계 — v1은 seg 프레임 전체를 후보별로 RAM에
복사해 UAT 해상도(1080×2046)에서 러너 메모리(16GB)를 초과, GitHub 러너가
OOM으로 종료됨(2026-08-22 04시대 3회 재현). v2는 프레임 단위 스트림 합성으로
피크 메모리를 수백 MB 수준으로 유지한다.

구조 (production H.2/H.3와 동일 순서를 파이프라인 안에서 그대로 사용):
  segment_v32(residual_export=1) — 카드 un-blend → flow 실화소 전파 →
    residual 조각(crop+context)만 npz 저장·자리채움 → Preserver
  → 각 조각을 SVOR(F)/순정 VACE(D)로 복원
  → hole alpha(feather 2px)로 seg 스트림에 프레임 단위 되붙여 cand_D/F 생성

HYBRID_SPEC (JSON): [{"pid","roi","t0","t1","cands":["D","F"]}]
산출: bench-assets/tournament/{roi}/cand_D.mp4 · cand_F.mp4 · hybrid_meta.json
"""
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
    for att in range(3):
        try:
            with requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                              headers=hdr(), stream=True, timeout=1800) as r:
                if r.status_code in (400, 404):
                    return None
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for c in r.iter_content(1 << 20):
                        f.write(c)
            return os.path.getsize(dst)
        except Exception as e:  # noqa: BLE001 — 일시 오류(520 등) 재시도
            if att == 2:
                print(f"[HYB] dl 실패 {path}: {type(e).__name__}: {e}",
                      flush=True)
                return None
            import time as _t
            _t.sleep(5 * (att + 1))


def up(src, path, ctype="video/mp4"):
    for att in range(3):
        try:
            with open(src, "rb") as f:
                r = requests.post(
                    f"{SB_URL}/storage/v1/object/videos-clips/{path}",
                    headers=hdr({"Content-Type": ctype, "x-upsert": "true"}),
                    data=f.read(), timeout=1800)
            r.raise_for_status()
            return
        except Exception as e:  # noqa: BLE001 — 일시 오류(520 등) 재시도
            if att == 2:
                raise
            print(f"[HYB] up 재시도 {path}: {type(e).__name__}", flush=True)
            import time as _t
            _t.sleep(5 * (att + 1))


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


class StreamEncoder:
    def __init__(self, path, w, h, fps, crf=12):
        self.p = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
             "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
             "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
             path, "-y"], stdin=subprocess.PIPE)

    def write(self, fr):
        self.p.stdin.write(fr.tobytes())

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        return self.p.returncode == 0


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
        print(f"[HYB] scan pid={pid} cands={cands}", flush=True)
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
        print(f"[HYB] parts={need} fps={fps} N={N}", flush=True)
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
                  f"counters={json.dumps(segstats[k])[:300]}", flush=True)
            if "error" in r:
                fail += 1
        # seg 출력은 파일로만 보관 (RAM 로드 금지 — v2 핵심)
        segpath = {}
        for k in need:
            sv = os.path.join(tmpd, f"seg_{k}.mp4")
            if dl("videos-clips", f"wmtmp-v32/{pid}/seg_{k}.mp4", sv):
                segpath[k] = sv
        resids = [n for n in ls_prefix(f"wmtmp-v32/{pid}")
                  if n.startswith("resid_") and n.endswith(".npz")
                  and int(n.split("_")[1]) in need]
        print(f"[HYB] resid packs={len(resids)}", flush=True)
        packs = []   # 조각 메타 + hole packbits + gen 파일 경로 (프레임 미보유)
        jobs = []    # (rec, cand, ev) — v3 병렬 실행용
        for nm in sorted(resids):
            rp = os.path.join(tmpd, nm)
            if not dl("videos-clips", f"wmtmp-v32/{pid}/{nm}", rp):
                continue
            z = np.load(rp)
            meta = json.loads(bytes(z["meta"]).decode())
            cs, ce = meta["cs"], meta["ce"]
            hh, ww = meta["shape"]
            n = ce - cs + 1
            frames, holes_pb, hole_px = [], [], 0
            for j in range(n):
                frames.append(cv2.imdecode(z[f"f{j}"], cv2.IMREAD_COLOR))
                holes_pb.append(np.array(z[f"m{j}"]))
                hole_px += int(np.unpackbits(z[f"m{j}"])[:hh * ww].sum())
            inp = os.path.join(tmpd, nm + ".in.mp4")
            mkp = os.path.join(tmpd, nm + ".mask.mp4")
            write_mp4(frames, inp, fps)
            write_mp4([cv2.cvtColor(
                (np.unpackbits(pb)[:hh * ww].reshape(hh, ww) * 255
                 ).astype(np.uint8), cv2.COLOR_GRAY2BGR)
                for pb in holes_pb], mkp, fps)
            del frames
            key_in = f"wmtmp-v32/{pid}/{nm}.in.mp4"
            key_mk = f"wmtmp-v32/{pid}/{nm}.mask.mp4"
            up(inp, key_in)
            up(mkp, key_mk)
            os.remove(inp)
            rec = {"pack": nm, "meta": meta, "holes_pb": holes_pb,
                   "hole_px": hole_px, "gen": {}}
            for cd in cands:
                out_key = f"wmtmp-v32/{pid}/{nm}.out_{cd}.mp4"
                ev = {"op": "roi", "video": key_in, "mask": key_mk,
                      "out": out_key, "lora": LORA[cd], "frames": n,
                      "dilation": 2, "steps": 20,
                      "max_area": min(720 * 1280, hh * ww)}
                jobs.append((rec, cd, ev))
            packs.append(rec)
        # v3: 생성 호출 병렬 window (기본 6) — 품질·GPU초 동일, 벽시계만 단축.
        # uat02 실측(조각 54개, 조각당 최대 824s)이 순차로는 러너 5h 한도를
        # 넘김이 확실해 병렬화. 대표 협의(2026-08-22): 한도는 분할·병렬로
        # 해결하고 폭주 방지 장치는 유지.
        WINDOW = int(os.environ.get("HYB_PAR", "6"))
        inflight = []
        ji = 0
        while ji < len(jobs) or inflight:
            while ji < len(jobs) and len(inflight) < WINDOW:
                rec2, cd2, ev2 = jobs[ji]
                try:
                    inflight.append((rec2, cd2, svor_fn.spawn(ev2)))
                except Exception as e:  # noqa: BLE001
                    print(f"[HYB] pack={rec2['pack']} cand={cd2} spawn 실패 "
                          f"{type(e).__name__}: {e}", flush=True)
                    fail += 1
                ji += 1
            if not inflight:
                break
            rec2, cd2, call = inflight.pop(0)
            try:
                res = call.get(timeout=3600)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            print(f"[HYB] pack={rec2['pack']} cand={cd2} -> "
                  f"{json.dumps({x: res.get(x) for x in ('ok', 'run_s', 'vram_gb', 'error')})}",
                  flush=True)
            if not res.get("ok"):
                fail += 1
                continue
            out_key2 = f"wmtmp-v32/{pid}/{rec2['pack']}.out_{cd2}.mp4"
            op2 = os.path.join(tmpd, rec2["pack"] + f".out_{cd2}.mp4")
            if dl("videos-clips", out_key2, op2):
                rec2["gen"][cd2] = op2
        # ---- 스트리밍 합성: 프레임 단위 (피크 메모리 수백 MB) ----
        by_part = {}
        for rec in packs:
            by_part.setdefault(rec["meta"]["part"], []).append(rec)
        for it in items:
            f0, f1, roi = it["f0"], it["f1"], it["roi"]
            spans = [(k, a, b) for k, (a, b) in enumerate(segments)
                     if f0 < b and f1 > a]
            for cd in it.get("cands", ["D", "F"]):
                if not all(k in segpath for k, _a, _b in spans):
                    print(f"[HYB] roi={roi} cand={cd} seg 누락")
                    fail += 1
                    continue
                outp = os.path.join(tmpd, f"{roi}_cand_{cd}.mp4")
                enc = StreamEncoder(outp, W, H, fps)
                nw = 0
                for k, a, b in spans:
                    # 이 part 조각의 gen 프레임을 지금만 로드
                    gens = {}
                    for rec in by_part.get(k, []):
                        gp = rec["gen"].get(cd)
                        if gp:
                            hh, ww = rec["meta"]["shape"]
                            gf = read_frames(gp)
                            gens[rec["pack"]] = [
                                (cv2.resize(g, (ww, hh),
                                            interpolation=cv2.INTER_LANCZOS4)
                                 if g.shape[:2] != (hh, ww) else g)
                                for g in gf]
                    cap = cv2.VideoCapture(segpath[k])
                    gi = a
                    while True:
                        ok2, fr = cap.read()
                        if not ok2:
                            break
                        if f0 <= gi < f1:
                            for rec in by_part.get(k, []):
                                m = rec["meta"]
                                j = gi - m["E0"] - m["cs"]
                                gl = gens.get(rec["pack"])
                                if gl is None or j < 0 or j >= len(gl) \
                                        or j >= len(rec["holes_pb"]):
                                    continue
                                hh, ww = m["shape"]
                                x0c, y0c, x1c, y1c = m["crop"]
                                rx, ry = m["reg_xy"]
                                hole = (np.unpackbits(rec["holes_pb"][j])
                                        [:hh * ww].reshape(hh, ww)
                                        * 255).astype(np.uint8)
                                if not hole.any():
                                    continue
                                al = cv2.GaussianBlur(hole, (0, 0), 2) \
                                    .astype(np.float32)[..., None] / 255.0
                                gy0, gy1 = ry + y0c, ry + y1c
                                gx0, gx1 = rx + x0c, rx + x1c
                                sub = fr[gy0:gy1, gx0:gx1].astype(np.float32)
                                fr[gy0:gy1, gx0:gx1] = np.clip(
                                    sub * (1 - al)
                                    + gl[j].astype(np.float32) * al,
                                    0, 255).astype(np.uint8)
                            enc.write(fr)
                            nw += 1
                        gi += 1
                        if gi >= f1:
                            break
                    cap.release()
                    del gens
                if not enc.close():
                    print(f"[HYB] roi={roi} cand_{cd} 인코딩 실패")
                    fail += 1
                    continue
                up(outp, f"{DEST}/{roi}/cand_{cd}.mp4")
                print(f"[HYB] roi={roi} cand_{cd} frames={nw} "
                      f"sha256={sha(outp)}", flush=True)
                os.remove(outp)
            covs = [segstats.get(k, {}) for k, _a, _b in spans]
            used = sum(c.get("flow_used", 0) for c in covs)
            csum = sum(c.get("flow_cover_pct_sum", 0) for c in covs)
            hmeta = {"roi": roi,
                     "flow_used_chunks": used,
                     "flow_bypass_chunks": sum(c.get("flow_bypass", 0)
                                               for c in covs),
                     "real_pixel_coverage_avg_pct":
                         round(csum / used, 1) if used else None,
                     "resid_packs": len(packs),
                     "resid_hole_px": sum(p["hole_px"] for p in packs)}
            mf = os.path.join(tmpd, f"{roi}_hybrid_meta.json")
            open(mf, "w").write(json.dumps(hmeta))
            up(mf, f"{DEST}/{roi}/hybrid_meta.json", "application/json")
            print(f"[HYB] roi={roi} meta={json.dumps(hmeta)}", flush=True)
    print(f"[HYB] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
