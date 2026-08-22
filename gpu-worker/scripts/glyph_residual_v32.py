# -*- coding: utf-8 -*-
"""RC4 P7 보조지표 — 글자잔상(residual glyph) 객관 계측.

왜 필요한가 (실측 근거):
  PROVE RC-S/RC-T는 "지운 자리가 주변과 자연스럽게 이어지는가"를 본다.
  그런데 명세 11.2의 **필수 탈락 1순위는 "읽을 수 있는 글자잔상"**이고,
  실측에서 PROVE 상위 후보(D/F)가 육안으로는 카드 자막 구간에서 글자
  잔상을 남기는 사례가 확인됐다. 즉 PROVE만으로는 이 탈락 조건을 못 잡는다.
  → 원본 자막의 획(glyph) 위치를 알고 있으므로, 결과 영상에서 그 획 위에
    남은 구조 에너지를 직접 재는 지표를 따로 만든다.

계측 방법 (정답 영상 불필요):
  1) 원본(input)에서 mask 안의 글자 획 위치 G = Canny(input) ∧ mask(침식)
  2) 같은 mask 안에서 획이 아닌 배경 위치 B = mask(침식) \ dilate(G)
  3) 후보 출력의 gradient magnitude를 두 집합에서 각각 평균
  4) glyph_ratio = mean|grad|(G) / mean|grad|(B)
     - 1.0 근처 = 글자 흔적 없음(획 위와 배경이 구분 안 됨) → 좋음
     - 값이 클수록 획 자리에 구조가 남아 있음 = 글자잔상 → 나쁨
  보조로 glyph_contrast = |mean(out[G]) - mean(out[B])| (밝기 잔상)

card_res (카드 잔존도, 명세 11.2 "카드 ghost" 대응):
  mask 안쪽 평균밝기 - mask 바깥 링 평균밝기 = gap.
  card_res = |gap(결과)| / |gap(원본)| → 0에 가까울수록 카드가 완전히 제거됨,
  1에 가까우면 카드가 그대로 남아 있음. (원본 gap이 3 미만이면 카드 없음으로 보고 생략)
  글자획 지표(glyph_ratio)는 획 모양 잔상만 잡고 넓은 면(카드) 잔존은 못 잡으므로 함께 본다.

GLYPH_SPEC (JSON): [{"roi","cands":[...],"frames":81}]
산출: 표준출력 [GLYPH] 행 + docs용 CSV 문자열
"""
import json
import os
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
                print(f"[GLYPH] dl 실패 {path}: {type(e).__name__}")
                return None
            import time as _t
            _t.sleep(5 * (att + 1))


def frames_of(path, nmax):
    cap = cv2.VideoCapture(path)
    out = []
    while len(out) < nmax:
        ok, fr = cap.read()
        if not ok:
            break
        out.append(fr)
    cap.release()
    return out


def grad_mag(bgr):
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def main():
    spec = json.loads(os.environ.get("GLYPH_SPEC", "[]"))
    if not spec:
        print("[GLYPH] empty GLYPH_SPEC")
        sys.exit(1)
    tmpd = tempfile.mkdtemp(prefix="glyph-")
    lines = ["roi,cand,glyph_ratio,glyph_contrast,card_res,frames,glyph_px,bg_px"]
    fail = 0
    for it in spec:
        roi = it["roi"]
        nmax = int(it.get("frames", 81))
        ip = os.path.join(tmpd, "input.mp4")
        mp = os.path.join(tmpd, "mask.mp4")
        if not dl(f"{SRC}/{roi}/input.mp4", ip) or \
                not dl(f"{SRC}/{roi}/mask.mp4", mp):
            print(f"[GLYPH] {roi} 원본/mask 없음")
            fail += 1
            continue
        inp = frames_of(ip, nmax)
        msk = frames_of(mp, nmax)
        n0 = min(len(inp), len(msk))
        # 프레임별 글자획 G / 배경 B 집합을 원본 기준으로 미리 계산
        Gs, Bs, Is, Rs, gapin = [], [], [], [], []
        for i in range(n0):
            m = cv2.cvtColor(msk[i], cv2.COLOR_BGR2GRAY)
            m = (m > 127).astype(np.uint8)
            m_in = cv2.erode(m, np.ones((5, 5), np.uint8), iterations=1)
            ring = (cv2.dilate(m, np.ones((25, 25), np.uint8), 1)
                    - cv2.dilate(m, np.ones((7, 7), np.uint8), 1))
            if m_in.sum() < 200 or ring.sum() < 200:
                Gs.append(None)
                Bs.append(None)
                Is.append(None)
                Rs.append(None)
                gapin.append(0.0)
                continue
            gin = cv2.cvtColor(inp[i], cv2.COLOR_BGR2GRAY).astype(np.float32)
            Is.append(m_in > 0)
            Rs.append(ring > 0)
            gapin.append(float(gin[m_in > 0].mean() - gin[ring > 0].mean()))
            gray = cv2.cvtColor(inp[i], cv2.COLOR_BGR2GRAY)
            ed = cv2.Canny(gray, 60, 160)
            G = ((ed > 0) & (m_in > 0))
            Gd = cv2.dilate(G.astype(np.uint8),
                            np.ones((3, 3), np.uint8), iterations=1)
            B = (m_in > 0) & (Gd == 0)
            if G.sum() < 50 or B.sum() < 50:
                Gs.append(None)
                Bs.append(None)
                continue
            Gs.append(G)
            Bs.append(B)
        valid = [i for i in range(n0) if Gs[i] is not None]
        if not valid:
            print(f"[GLYPH] {roi} 유효 프레임 0 (자막 획 미검출)")
            fail += 1
            continue
        gpx = int(np.mean([Gs[i].sum() for i in valid]))
        bpx = int(np.mean([Bs[i].sum() for i in valid]))
        for cd in it.get("cands", ["cand_A"]):
            cp = os.path.join(tmpd, f"{cd}.mp4")
            if not dl(f"{SRC}/{roi}/{cd}.mp4", cp):
                print(f"[GLYPH] {roi} {cd} 없음")
                continue
            out = frames_of(cp, nmax)
            n = min(len(out), n0)
            ratios, contrasts, cards = [], [], []
            for i in valid:
                if i >= n:
                    break
                gm = grad_mag(out[i])
                gray = cv2.cvtColor(out[i], cv2.COLOR_BGR2GRAY).astype(np.float32)
                gG = float(gm[Gs[i]].mean())
                gB = float(gm[Bs[i]].mean())
                if gB <= 1e-6:
                    continue
                ratios.append(gG / gB)
                contrasts.append(abs(float(gray[Gs[i]].mean())
                                     - float(gray[Bs[i]].mean())))
                if Is[i] is not None and abs(gapin[i]) > 3.0:
                    gout = float(gray[Is[i]].mean() - gray[Rs[i]].mean())
                    cards.append(min(2.0, abs(gout) / abs(gapin[i])))
            if not ratios:
                print(f"[GLYPH] {roi} {cd} 계측 실패")
                continue
            r = round(float(np.mean(ratios)), 4)
            c = round(float(np.mean(contrasts)), 3)
            cr = round(float(np.mean(cards)), 4) if cards else ""
            lines.append(f"{roi},{cd},{r},{c},{cr},{len(ratios)},{gpx},{bpx}")
            print(f"[GLYPH] {roi} {cd} glyph_ratio={r} contrast={c} "
                  f"card_res={cr} (G={gpx}px B={bpx}px, {len(ratios)}f)",
                  flush=True)
            os.remove(cp)
    print("[GLYPH] ---CSV---")
    for ln in lines:
        print(ln)
    print(f"[GLYPH] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
