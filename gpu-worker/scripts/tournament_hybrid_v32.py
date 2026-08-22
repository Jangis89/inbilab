# -*- coding: utf-8 -*-
"""후보 D/F 하이브리드 오케스트레이터 v4 (후속 명세 Phase 6 — 2차).

v4: residual 생성의 시간 창을 chunk(~12f) → part 병합 창(≤81f)으로 확대.
v1~v3.1의 실측 결론(진행 로그 D2): chunk 단위 생성은 시간 문맥이 부족해
골든 PSNR이 전체창 후보(C/E)보다 낮고, uat01_t154 육안에서 잔상이 남았다.
v4는 같은 (part, reg)의 residual 조각 hole을 프레임 축으로 병합한 뒤
spans_to_windows()로 ≤81프레임 창(overlap 8, pad 4)을 만들어 1회 생성한다.
생성 입력은 seg 출력 영상의 union crop이다 — 자리채움(inpaint) 화소는
VACE 전처리가 mask 영역을 회색으로 대체하므로 생성 품질에 영향이 없고,
hole 밖 화소(실화소 전파·Preserver 결과)가 시간 문맥으로 쓰인다.

유지되는 항목:
  - v2 스트리밍 합성(피크 메모리 수백 MB; 러너 16GB OOM 방지)
  - v3 생성 호출 병렬 window(HYB_PAR, 기본 6; 러너 5h 한도 대응 —
    대표 협의 2026-08-22: 한도는 분할·병렬로 해결, 폭주 방지는 유지)
  - v3.1 업/다운로드 3회 재시도(저장소 일시 오류 520 대응)
  - production H.2/H.3 동일 순서: 카드 un-blend → flow 실화소 전파 →
    residual hole만 생성 → Preserver (전체 사각형 생성 금지)

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
WIN_MAX = 81   # 생성 창 최대 프레임 (VACE 전체창과 동일 규모)
WIN_OVL = 8    # 인접 창 겹침 (합성 시 중앙 분할로 이음새 배분)
WIN_PAD = 4    # hole span 앞뒤 문맥 여유


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


def spans_to_windows(hole_ls, plen):
    """hole이 있는 part-local 프레임 목록 → 생성 창 [(w0,w1,c0,c1)].

    w0..w1 = 생성에 넣는 범위(pad 포함), c0..c1 = 합성에 쓰는 core 범위.
    인접 창은 WIN_OVL 겹치고 core는 겹침 중앙에서 나뉜다.
    """
    if not hole_ls:
        return []
    ls = sorted(hole_ls)
    spans = []
    s = e = ls[0]
    for l in ls[1:]:
        if l - e <= WIN_OVL:      # 작은 공백은 같은 span으로 병합
            e = l
        else:
            spans.append((s, e + 1))
            s = e = l
    spans.append((s, e + 1))
    padded = []
    for s, e in spans:
        s, e = max(0, s - WIN_PAD), min(plen, e + WIN_PAD)
        if padded and s <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], e))
        else:
            padded.append((s, e))
    wins = []
    stride = WIN_MAX - WIN_OVL
    for s, e in padded:
        if e - s <= WIN_MAX:
            wins.append([s, e])
        else:
            w0 = s
            while True:
                w1 = min(w0 + WIN_MAX, e)
                wins.append([w0, w1])
                if w1 >= e:
                    break
                w0 += stride
            # 마지막 창이 짧으면 뒤에서 WIN_MAX만큼 당겨 잡음
            # (짧은 창 = 시간 문맥 부족 = v1 품질 미달의 원인 — 방지)
            wins[-1][0] = max(s, wins[-1][1] - WIN_MAX)
        # core 분할: 같은 span 안의 인접 창끼리 겹침 중앙에서 나눔
    out = []
    for i, (w0, w1) in enumerate(wins):
        c0 = w0
        c1 = w1
        if i > 0 and wins[i - 1][1] > w0:            # 앞 창과 겹침
            c0 = w0 + (wins[i - 1][1] - w0) // 2
        if i + 1 < len(wins) and wins[i + 1][0] < w1:  # 뒤 창과 겹침
            c1 = wins[i + 1][0] + (w1 - wins[i + 1][0]) // 2
        out.append((w0, w1, c0, c1))
    return out


def main():
    spec = json.loads(os.environ.get("HYBRID_SPEC", "[]"))
    if not spec:
        print("[HYB] empty HYBRID_SPEC")
        sys.exit(1)
    # P8 속도최적화 A/B: spec[0]의 steps/suffix로 전 항목 공통 설정.
    # steps 감소는 명세 8.2에 따라 "품질 동등 증명" 후에만 채택한다.
    HYB_STEPS = int(spec[0].get("steps", 20))
    HYB_SUFFIX = str(spec[0].get("suffix", ""))
    print(f"[HYB] steps={HYB_STEPS} suffix='{HYB_SUFFIX}'", flush=True)
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
        # ---- v4: 조각을 (part, reg)별로 병합 (hole·crop만 사용; 조각의
        #          residual 프레임 PNG는 v4에서 불필요 — 생성 입력은 seg 영상) ----
        packs = []
        groups = {}
        for nm in sorted(resids):
            rp = os.path.join(tmpd, nm)
            if not dl("videos-clips", f"wmtmp-v32/{pid}/{nm}", rp):
                continue
            z = np.load(rp)
            meta = json.loads(bytes(z["meta"]).decode())
            hh, ww = meta["shape"]
            n = meta["ce"] - meta["cs"] + 1
            holes_pb = [np.array(z[f"m{j}"]) for j in range(n)]
            hole_px = sum(int(np.unpackbits(pb)[:hh * ww].sum())
                          for pb in holes_pb)
            rec = {"pack": nm, "meta": meta, "holes_pb": holes_pb,
                   "hole_px": hole_px}
            packs.append(rec)
            groups.setdefault((meta["part"], meta["reg"]), []).append(rec)
            os.remove(rp)
        wrecs = []   # 창 레코드 (part별 합성에 사용)
        jobs = []    # (wrec, cand, ev) — v3 병렬 실행용
        for (k, reg), grp in sorted(groups.items()):
            a, b = segments[k]
            plen = b - a
            # union crop (global 좌표)
            gx0 = gy0 = 10 ** 9
            gx1 = gy1 = -1
            for rec in grp:
                m = rec["meta"]
                x0c, y0c, x1c, y1c = m["crop"]
                rx, ry = m["reg_xy"]
                gx0, gy0 = min(gx0, rx + x0c), min(gy0, ry + y0c)
                gx1, gy1 = max(gx1, rx + x1c), max(gy1, ry + y1c)
            gx0, gy0 = max(0, gx0), max(0, gy0)
            gx1, gy1 = min(W, gx1), min(H, gy1)
            uw, uh = gx1 - gx0, gy1 - gy0
            if uw <= 0 or uh <= 0:
                continue
            # part-local 프레임 l → union-crop hole mask (packbits)
            hl = {}
            for rec in grp:
                m = rec["meta"]
                hh, ww = m["shape"]
                x0c, y0c, x1c, y1c = m["crop"]
                rx, ry = m["reg_xy"]
                oy, ox = ry + y0c - gy0, rx + x0c - gx0
                for j, pb in enumerate(rec["holes_pb"]):
                    l = m["E0"] + m["cs"] + j - a
                    if not (0 <= l < plen):
                        continue
                    hole = np.unpackbits(pb)[:hh * ww].reshape(hh, ww)
                    if not hole.any():
                        continue
                    cnv = (np.unpackbits(hl[l])[:uh * uw].reshape(uh, uw)
                           if l in hl else
                           np.zeros((uh, uw), np.uint8))
                    ey, ex = min(uh, oy + hh), min(uw, ox + ww)
                    if ey > max(0, oy) and ex > max(0, ox):
                        cnv[max(0, oy):ey, max(0, ox):ex] |= \
                            hole[max(0, -oy):ey - oy, max(0, -ox):ex - ox]
                    hl[l] = np.packbits(cnv)
            wins = spans_to_windows(list(hl.keys()), plen)
            print(f"[HYB] group part={k} reg={reg} rect="
                  f"({gx0},{gy0},{gx1},{gy1}) hole_frames={len(hl)} "
                  f"windows={[(w0, w1) for w0, w1, _c0, _c1 in wins]}",
                  flush=True)
            if not wins or k not in segpath:
                if wins:
                    print(f"[HYB] part={k} seg 영상 누락 — 창 {len(wins)}개 skip")
                    fail += len(wins)
                continue
            for w0, w1, c0, c1 in wins:
                tag = f"hybw_{k}_{reg}_{w0}"
                # 생성 입력 = seg 출력 영상의 union crop (w0..w1)
                cap = cv2.VideoCapture(segpath[k])
                cap.set(cv2.CAP_PROP_POS_FRAMES, w0)
                inf, mkf = [], []
                for l in range(w0, w1):
                    ok2, fr = cap.read()
                    if not ok2:
                        break
                    inf.append(fr[gy0:gy1, gx0:gx1].copy())
                    pb = hl.get(l)
                    hole = (np.unpackbits(pb)[:uh * uw].reshape(uh, uw) * 255
                            ).astype(np.uint8) if pb is not None else \
                        np.zeros((uh, uw), np.uint8)
                    mkf.append(cv2.cvtColor(hole, cv2.COLOR_GRAY2BGR))
                cap.release()
                if len(inf) < w1 - w0:
                    print(f"[HYB] {tag} seg 프레임 부족 "
                          f"{len(inf)}/{w1 - w0} — skip")
                    fail += 1
                    continue
                inp = os.path.join(tmpd, tag + ".in.mp4")
                mkp = os.path.join(tmpd, tag + ".mask.mp4")
                write_mp4(inf, inp, fps)
                write_mp4(mkf, mkp, fps)
                del inf, mkf
                key_in = f"wmtmp-v32/{pid}/{tag}.in.mp4"
                key_mk = f"wmtmp-v32/{pid}/{tag}.mask.mp4"
                up(inp, key_in)
                up(mkp, key_mk)
                os.remove(inp)
                os.remove(mkp)
                wrec = {"tag": tag, "part": k, "reg": reg,
                        "rect": (gx0, gy0, gx1, gy1),
                        "w0": w0, "w1": w1, "c0": c0, "c1": c1,
                        "holes": hl, "gen": {}}
                for cd in cands:
                    out_key = f"wmtmp-v32/{pid}/{tag}.out_{cd}{HYB_SUFFIX}.mp4"
                    ev = {"op": "roi", "video": key_in, "mask": key_mk,
                          "out": out_key, "lora": LORA[cd],
                          "frames": w1 - w0, "dilation": 2, "steps": HYB_STEPS,
                          "max_area": min(720 * 1280, uh * uw)}
                    jobs.append((wrec, cd, ev))
                wrecs.append(wrec)
        # v3: 생성 호출 병렬 window (기본 6) — 품질·GPU초 동일, 벽시계만 단축.
        WINDOW = int(os.environ.get("HYB_PAR", "6"))
        inflight = []
        ji = 0
        while ji < len(jobs) or inflight:
            while ji < len(jobs) and len(inflight) < WINDOW:
                rec2, cd2, ev2 = jobs[ji]
                try:
                    inflight.append((rec2, cd2, svor_fn.spawn(ev2)))
                except Exception as e:  # noqa: BLE001
                    print(f"[HYB] {rec2['tag']} cand={cd2} spawn 실패 "
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
            print(f"[HYB] {rec2['tag']} cand={cd2} -> "
                  f"{json.dumps({x: res.get(x) for x in ('ok', 'run_s', 'vram_gb', 'error')})}",
                  flush=True)
            if not res.get("ok"):
                fail += 1
                continue
            out_key2 = f"wmtmp-v32/{pid}/{rec2['tag']}.out_{cd2}{HYB_SUFFIX}.mp4"
            op2 = os.path.join(tmpd, rec2["tag"] + f".out_{cd2}{HYB_SUFFIX}.mp4")
            if dl("videos-clips", out_key2, op2):
                rec2["gen"][cd2] = op2
        # ---- 스트리밍 합성: 프레임 단위 (피크 메모리 수백 MB) ----
        by_part = {}
        for wrec in wrecs:
            by_part.setdefault(wrec["part"], []).append(wrec)
        for it in items:
            f0, f1, roi = it["f0"], it["f1"], it["roi"]
            spans = [(k, a, b) for k, (a, b) in enumerate(segments)
                     if f0 < b and f1 > a]
            for cd in it.get("cands", ["D", "F"]):
                if not all(k in segpath for k, _a, _b in spans):
                    print(f"[HYB] roi={roi} cand={cd} seg 누락")
                    fail += 1
                    continue
                outp = os.path.join(tmpd, f"{roi}_cand_{cd}{HYB_SUFFIX}.mp4")
                enc = StreamEncoder(outp, W, H, fps)
                nw = 0
                for k, a, b in spans:
                    gens = {}   # tag → 프레임 리스트 (창 core 통과 시 해제)
                    cap = cv2.VideoCapture(segpath[k])
                    gi = a
                    while True:
                        ok2, fr = cap.read()
                        if not ok2:
                            break
                        l = gi - a
                        if f0 <= gi < f1:
                            for wrec in by_part.get(k, []):
                                if not (wrec["c0"] <= l < wrec["c1"]):
                                    continue
                                gp = wrec["gen"].get(cd)
                                pb = wrec["holes"].get(l)
                                if gp is None or pb is None:
                                    continue
                                if wrec["tag"] not in gens:
                                    gens[wrec["tag"]] = read_frames(gp)
                                gl = gens[wrec["tag"]]
                                j = l - wrec["w0"]
                                if j < 0 or j >= len(gl):
                                    continue
                                gx0, gy0, gx1, gy1 = wrec["rect"]
                                uh, uw = gy1 - gy0, gx1 - gx0
                                g = gl[j]
                                if g.shape[:2] != (uh, uw):
                                    if g.shape[0] >= uh and g.shape[1] >= uw:
                                        g = g[:uh, :uw]   # 짝수 pad 제거
                                    else:
                                        g = cv2.resize(
                                            g, (uw, uh),
                                            interpolation=cv2.INTER_LANCZOS4)
                                hole = (np.unpackbits(pb)[:uh * uw]
                                        .reshape(uh, uw) * 255).astype(np.uint8)
                                if not hole.any():
                                    continue
                                al = cv2.GaussianBlur(hole, (0, 0), 2) \
                                    .astype(np.float32)[..., None] / 255.0
                                sub = fr[gy0:gy1, gx0:gx1].astype(np.float32)
                                fr[gy0:gy1, gx0:gx1] = np.clip(
                                    sub * (1 - al)
                                    + g.astype(np.float32) * al,
                                    0, 255).astype(np.uint8)
                            enc.write(fr)
                            nw += 1
                        # core를 지난 창의 gen 프레임 즉시 해제 (RAM 절약)
                        for wrec in by_part.get(k, []):
                            if wrec["tag"] in gens and l >= wrec["c1"]:
                                del gens[wrec["tag"]]
                        gi += 1
                        if gi >= f1:
                            break
                    cap.release()
                    del gens
                if not enc.close():
                    print(f"[HYB] roi={roi} cand_{cd} 인코딩 실패")
                    fail += 1
                    continue
                up(outp, f"{DEST}/{roi}/cand_{cd}{HYB_SUFFIX}.mp4")
                print(f"[HYB] roi={roi} cand_{cd}{HYB_SUFFIX} frames={nw} "
                      f"sha256={sha(outp)}", flush=True)
                os.remove(outp)
            covs = [segstats.get(k, {}) for k, _a, _b in spans]
            used = sum(c.get("flow_used", 0) for c in covs)
            csum = sum(c.get("flow_cover_pct_sum", 0) for c in covs)
            hmeta = {"roi": roi, "hybrid_ver": "v4", "steps": HYB_STEPS,
                     "flow_used_chunks": used,
                     "flow_bypass_chunks": sum(c.get("flow_bypass", 0)
                                               for c in covs),
                     "real_pixel_coverage_avg_pct":
                         round(csum / used, 1) if used else None,
                     "resid_packs": len(packs),
                     "resid_hole_px": sum(p["hole_px"] for p in packs),
                     "gen_windows": len(wrecs)}
            mf = os.path.join(tmpd, f"{roi}_hybrid_meta{HYB_SUFFIX}.json")
            open(mf, "w").write(json.dumps(hmeta))
            up(mf, f"{DEST}/{roi}/hybrid_meta{HYB_SUFFIX}.json", "application/json")
            print(f"[HYB] roi={roi} meta={json.dumps(hmeta)}", flush=True)
    print(f"[HYB] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
