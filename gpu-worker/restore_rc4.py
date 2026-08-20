# -*- coding: utf-8 -*-
"""RC4 실화소 우선 하이브리드 복원 엔진 (명세 REV2 Phase C/E/F).

원칙:
 1. 생성AI 이전에 앞뒤 프레임의 '진짜 화소'를 먼저 쓴다 (multi-reference
    bidirectional flow propagation).
 2. effect mask 밖 원본은 강제 보존한다 (Preserver — 절대 자물쇠).
 3. 실화소로 못 메운 residual hole만 생성모델에 넘긴다.

의존: numpy, cv2 (DIS optical flow — Apache-2.0). GPU 불필요(CPU).
handler_v32.segment_v32 에서 chunk 단위로 호출된다.
"""
import numpy as np
import cv2

# ---------------- 파라미터 (명세 E) ----------------
REF_OFFSETS = (1, 2, 3, 5, 8, 12, 20, 40)   # 참조 프레임 거리 (양방향)
FB_MAX_ERR = 5.0                   # fwd-bwd 왕복 오차 상한 (px) — 이보다 크면 무효
MIN_VALID_W = 0.15                 # 픽셀 융합 최소 가중 합 — 미만이면 residual hole
EDGE_MARGIN = 2                    # 참조 프레임 경계 마진


def _dis():
    f = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    f.setUseSpatialPropagation(True)
    return f


def _flow_pair(g_dst, g_src, engine=None, half=True):
    """dst→src 방향 flow (dst 픽셀이 src 어디서 왔는지). 반환 HxWx2 (x,y).

    half=True면 0.5×에서 추정 후 업스케일 (DIS 4× 가속, 게이트로 오차 방어)."""
    e = engine or _dis()
    if half and min(g_dst.shape[:2]) >= 128:
        h, w = g_dst.shape[:2]
        d2 = cv2.resize(g_dst, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        s2 = cv2.resize(g_src, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        f2 = e.calc(d2, s2, None)
        f = cv2.resize(f2, (w, h), interpolation=cv2.INTER_LINEAR) * 2.0
        return f
    return e.calc(g_dst, g_src, None)


def _warp_from(src_img, flow):
    """flow(dst→src)로 src_img를 dst 좌표계로 당겨온다."""
    h, w = flow.shape[:2]
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32),
                         np.arange(h, dtype=np.float32))
    mx = gx + flow[..., 0]
    my = gy + flow[..., 1]
    warped = cv2.remap(src_img, mx, my, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return warped, mx, my


def _sample_mask(mask_u8, mx, my):
    """참조 프레임 마스크를 flow 좌표로 샘플 (원천이 오염 영역인지 판단)."""
    return cv2.remap(mask_u8, mx, my, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def _fill_flow(flow, mask_u8, down=4, iters=40):
    """마스크 내부 flow를 주변 flow의 확산으로 보간 (flow completion).

    오버레이 위에서 추정된 flow는 무의미하므로, 밖의 운동장(카메라 팬·배경
    이동)을 매끄럽게 이어붙인다. 저해상 확산 → 원해상 복원."""
    if mask_u8 is None or not mask_u8.any():
        return flow
    h, w = flow.shape[:2]
    hs, ws = max(2, h // down), max(2, w // down)
    fs = cv2.resize(flow, (ws, hs), interpolation=cv2.INTER_AREA)
    ms = cv2.resize((mask_u8 > 127).astype(np.float32), (ws, hs),
                    interpolation=cv2.INTER_AREA)
    valid = (ms < 0.5).astype(np.float32)
    vals = fs * valid[..., None]
    k = np.ones((3, 3), np.float32)
    vv, va = vals.copy(), valid.copy()
    for _ in range(iters):
        num = cv2.filter2D(vv, -1, k)
        den = cv2.filter2D(va, -1, k[..., 0] if k.ndim == 3 else k)
        newly = (va < 0.5) & (den > 1e-3)
        if newly.any():
            vv[newly] = num[newly] / den[newly][..., None]
            va[newly] = 1.0
        # 내부도 서서히 평활 (경계 고정)
        smooth = num / np.maximum(den, 1e-3)[..., None]
        inner = ms > 0.5
        vv[inner] = smooth[inner]
        if va.min() > 0.5 and _ > 8:
            break
    filled = cv2.resize(vv, (w, h), interpolation=cv2.INTER_LINEAR)
    out = flow.copy()
    mm = mask_u8 > 127
    out[mm] = filled[mm]
    return out


def _neutral_gray(frame, mask):
    """flow 추정용 회색조 — 마스크(오버레이)를 TELEA로 중화해 flow가
    오버레이 자체에 잠기는 것을 막는다 (flow completion 효과)."""
    g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    if mask is not None and mask.any():
        mb = cv2.dilate((mask > 127).astype(np.uint8), np.ones((5, 5), np.uint8))
        g = cv2.inpaint(g, mb, 7, cv2.INPAINT_TELEA)
    return g


def propagate_frame(frames, masks, ti, offsets=REF_OFFSETS,
                    fb_max_err=FB_MAX_ERR, engine=None, gray_cache=None,
                    prefer_unmasked_src=True):
    """프레임 ti의 마스크 영역을 앞뒤 실화소로 복원.

    frames: list[HxWx3 uint8] (필요 시 un-blend 선적용본)
    masks:  list[HxW uint8 or None] — 255=복원 대상
    반환: (filled float32 HxWx3, weight float32 HxW[0..1], hole u8 HxW)
    """
    n = len(frames)
    m = masks[ti]
    h, w = frames[ti].shape[:2]
    acc = np.zeros((h, w, 3), np.float32)
    wacc = np.zeros((h, w), np.float32)
    if m is None or not m.any():
        return acc, wacc, np.zeros((h, w), np.uint8)
    if gray_cache is None:
        gray_cache = {}

    def gray(i):
        if i not in gray_cache:
            gray_cache[i] = _neutral_gray(frames[i],
                                          masks[i] if i < len(masks) else None)
        return gray_cache[i]

    tgt_g = gray(ti)
    need = m > 127
    mb = need.astype(np.uint8)
    # 링(마스크 밖 검증 밴드): warp 정확도를 실측할 수 있는 유일한 곳
    ring = (cv2.dilate(mb, np.ones((17, 17), np.uint8)) - mb).astype(bool)
    tgt_f = frames[ti].astype(np.float32)
    eng = engine or _dis()
    for off in offsets:
        # 조기 종료: 이미 충분히 덮였으면 먼 참조는 생략 (속도)
        if (wacc[need] >= MIN_VALID_W).mean() > 0.985:
            break
        for sj in (ti - off, ti + off):
            if sj < 0 or sj >= n or sj == ti:
                continue
            src_g = gray(sj)
            fl = _flow_pair(tgt_g, src_g, eng)            # ti→sj
            bl = _flow_pair(src_g, tgt_g, eng)            # sj→ti (왕복 검증)
            # flow completion: 오버레이 내부 flow를 주변 운동장으로 대체
            fl = _fill_flow(fl, m)
            if sj < len(masks) and masks[sj] is not None and masks[sj].any():
                bl = _fill_flow(bl, masks[sj])
            warped, mx, my = _warp_from(frames[sj].astype(np.float32), fl)
            # 왕복 오차: fl로 간 위치에서 bl을 샘플해 되돌아왔을 때의 편차
            blx, _, _ = _warp_from(bl, fl)
            err = np.hypot(fl[..., 0] + blx[..., 0], fl[..., 1] + blx[..., 1])
            conf = np.exp(-err / fb_max_err).astype(np.float32)
            # 링 광도 검증: 마스크 밖 검증 밴드에서 warp가 실제로 맞는가 —
            # 밴드 오차가 크면 이 참조 전체를 강등 (gross misalignment 게이트)
            if ring.any():
                band_err = float(np.abs(warped - tgt_f).max(axis=2)[ring].mean())
                ring_gate = float(np.exp(-max(0.0, band_err - 5.0) / 8.0))
            else:
                ring_gate = 1.0
            if ring_gate < 0.2:
                continue
            # 원천 유효성: 참조 프레임의 같은 위치가 마스크 밖이어야 실화소
            if prefer_unmasked_src and masks[sj] is not None and masks[sj].any():
                src_m = _sample_mask(masks[sj], mx, my)
                conf = conf * np.clip(1.0 - src_m.astype(np.float32) / 255.0,
                                      0.0, 1.0)
            # 프레임 경계 밖 샘플 무효
            oob = ((mx < EDGE_MARGIN) | (mx > w - 1 - EDGE_MARGIN)
                   | (my < EDGE_MARGIN) | (my > h - 1 - EDGE_MARGIN))
            conf[oob] = 0.0
            # 거리 가중 (가까운 참조 우선) × 링 게이트
            dist_w = 1.0 / (1.0 + 0.15 * off)
            wgt = conf * (dist_w * ring_gate)
            wgt[~need] = 0.0
            acc += warped * wgt[..., None]
            wacc += wgt
    filled = np.zeros_like(acc)
    nz = wacc > 1e-6
    filled[nz] = acc[nz] / wacc[nz][..., None]
    hole = ((wacc < MIN_VALID_W) & need).astype(np.uint8) * 255
    return filled, np.clip(wacc, 0, 1), hole


def restore_chunk_flow(frames, masks, chunk, ai_fallback=None, tier=None,
                       offsets=REF_OFFSETS, min_flow_cover=0.15,
                       stats=None, bbox_margin=64):
    """마스크 union bbox+margin으로 잘라 전파 후 되붙임 (속도 래퍼)."""
    s0, e0 = chunk["s"], min(chunk["e"], len(frames) - 1)
    h, w = frames[0].shape[:2]
    bb = None
    for k in range(s0, e0 + 1):
        m = masks[k] if k < len(masks) else None
        if m is None or not m.any():
            continue
        ys, xs = np.nonzero(m > 127)
        b2 = [xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]
        bb = b2 if bb is None else [min(bb[0], b2[0]), min(bb[1], b2[1]),
                                    max(bb[2], b2[2]), max(bb[3], b2[3])]
    if bb is None:
        return [frames[k].copy() for k in range(s0, e0 + 1)]
    x0 = max(0, int(bb[0]) - bbox_margin); y0 = max(0, int(bb[1]) - bbox_margin)
    x1 = min(w, int(bb[2]) + bbox_margin); y1 = min(h, int(bb[3]) + bbox_margin)
    if (x1 - x0) * (y1 - y0) < 0.85 * w * h:
        sub_f = [f[y0:y1, x0:x1] for f in frames]
        sub_m = [(m[y0:y1, x0:x1] if m is not None else None) for m in masks]
        def _sub_ai(fr2, mk2, tier2, ch2):
            if ai_fallback is None:
                return None
            full_f = [frames[i].copy() for i in range(len(frames))]
            full_m = [None] * len(frames)
            for i in range(len(frames)):
                full_f[i][y0:y1, x0:x1] = fr2[i]
                if mk2[i] is not None and mk2[i].any():
                    fm = np.zeros((h, w), np.uint8)
                    fm[y0:y1, x0:x1] = mk2[i]
                    full_m[i] = fm
            arr = ai_fallback(full_f, full_m, tier2, ch2)
            return [a[y0:y1, x0:x1] for a in arr]
        sub_out = _restore_chunk_flow_core(sub_f, sub_m, chunk,
                                           _sub_ai if ai_fallback else None,
                                           tier, offsets, min_flow_cover, stats)
        out = []
        for k in range(s0, e0 + 1):
            fr = frames[k].copy()
            fr[y0:y1, x0:x1] = sub_out[k - s0]
            out.append(fr)
        return out
    return _restore_chunk_flow_core(frames, masks, chunk, ai_fallback, tier,
                                    offsets, min_flow_cover, stats)


def _restore_chunk_flow_core(frames, masks, chunk, ai_fallback=None, tier=None,
                             offsets=REF_OFFSETS, min_flow_cover=0.15,
                             stats=None):
    """chunk 범위 [s..e]를 실화소 우선으로 복원.

    frames/masks: 세그먼트-로컬 전체 (참조 뱅크로 chunk 밖 프레임도 사용)
    ai_fallback(frames, masks, tier, chunk)->list: residual hole 전용 생성모델
      (None이면 hole은 cv2.inpaint로 메움 — 시험용)
    반환: chunk 길이의 복원 프레임 리스트 (h29.restore_chunk 호환)
    stats: dict면 flow_cover/hole_frac 등 계측치 기록
    """
    s, e = chunk["s"], min(chunk["e"], len(frames) - 1)
    n = e - s + 1
    eng = _dis()
    total_need = 0

    def _one_pass(fr_list, mk_list, cache, min_w=MIN_VALID_W):
        outp = [None] * n
        holep = [None] * n
        holed = 0
        for k in range(n):
            ti = s + k
            m = mk_list[ti] if ti < len(mk_list) else None
            base = fr_list[ti].astype(np.float32)
            if m is None or not m.any():
                outp[k] = fr_list[ti].copy()
                holep[k] = np.zeros(base.shape[:2], np.uint8)
                continue
            filled, wgt, hole = propagate_frame(fr_list, mk_list, ti,
                                                offsets=offsets, engine=eng,
                                                gray_cache=cache)
            need = (m > 127)
            # 유효 화소는 전량 교체 — 오염 원본과 혼합하면 잔상이 남는다
            merged = base.copy()
            valid = need & (wgt >= min_w)
            merged[valid] = filled[valid]
            merged[~need] = base[~need]
            outp[k] = np.clip(merged, 0, 255).astype(np.uint8)
            # hole은 수용 문턱과 반드시 일치시켜 재계산 (오버레이 잔존 방지)
            holep[k] = (need & ~valid).astype(np.uint8) * 255
            holed += int(holep[k].any() and (holep[k] > 0).sum() or 0)
            if len(cache) > 96:
                for kk in sorted(cache)[:32]:
                    del cache[kk]
        return outp, holep, holed

    for k in range(n):
        m0 = masks[s + k] if s + k < len(masks) else None
        if m0 is not None:
            total_need += int((m0 > 127).sum())
    # probe: 대표 5프레임만 먼저 전파해 flow 실효를 가늠 — 정지배경 상시
    # 오버레이(복원 불가)는 여기서 즉시 생성모델로 우회해 낭비를 막는다
    probe_idx = [s + int(n * q) for q in (0.1, 0.3, 0.5, 0.7, 0.9)]
    probe_idx = sorted({min(e, max(s, i2)) for i2 in probe_idx})
    p_need = p_hole = 0
    cache0 = {}
    for ti in probe_idx:
        m = masks[ti] if ti < len(masks) else None
        if m is None or not m.any():
            continue
        _, wgt, hole = propagate_frame(frames, masks, ti, offsets=offsets,
                                       engine=eng, gray_cache=cache0)
        p_need += int((m > 127).sum())
        p_hole += int((hole > 0).sum())
    if p_need > 0 and (1.0 - p_hole / p_need) < min_flow_cover:
        if stats is not None:
            stats["flow_bypass"] = 1
            stats["probe_cover"] = round(1.0 - p_hole / p_need, 4)
        if ai_fallback is not None:
            return ai_fallback(frames, masks, tier, chunk)
    out, holes, total_holed = _one_pass(frames, masks, cache0)
    # pass 2: 1차에서 채워진 실화소를 참조원으로 다시 전파 (시간축 체이닝)
    if total_holed > 0 and total_holed < total_need:
        fr2 = [f.copy() for f in frames]
        mk2 = [None] * len(frames)
        for k in range(n):
            fr2[s + k] = out[k]
            if holes[k] is not None and holes[k].any():
                mk2[s + k] = holes[k]
        # chunk 밖 프레임은 원 마스크 유지 (여전히 오염 가능)
        for i2 in range(len(frames)):
            if i2 < s or i2 > e:
                mk2[i2] = masks[i2] if i2 < len(masks) else None
        # 체이닝은 이중 warp 오차가 쌓이므로 더 엄격한 문턱으로만 수용
        out2, holes2, holed2 = _one_pass(fr2, mk2, {}, min_w=MIN_VALID_W * 3.0)
        for k in range(n):
            if mk2[s + k] is not None and mk2[s + k].any():
                out[k] = out2[k]
                holes[k] = holes2[k]
        total_holed = sum(int((h > 0).sum()) for h in holes if h is not None)
    flow_cover = 1.0 - (total_holed / max(1, total_need))
    if stats is not None:
        stats["flow_cover"] = round(flow_cover, 4)
        stats["need_px"] = total_need
        stats["hole_px"] = total_holed
    # residual hole 처리
    if total_holed > 0:
        if ai_fallback is not None and flow_cover >= min_flow_cover:
            # hole만 마스크로 생성모델 1회 호출 (chunk 국소)
            hmasks = [None] * len(frames)
            for k in range(n):
                if holes[k] is not None and holes[k].any():
                    hmasks[s + k] = cv2.dilate(holes[k],
                                               np.ones((5, 5), np.uint8))
            base_frames = [f.copy() for f in frames]
            for k in range(n):
                base_frames[s + k] = out[k]
            arr = ai_fallback(base_frames, hmasks, tier, chunk)
            for k in range(n):
                hm = hmasks[s + k]
                if hm is None or not hm.any():
                    continue
                a = cv2.GaussianBlur(hm, (0, 0), 2).astype(np.float32)[..., None] / 255.0
                out[k] = np.clip(out[k].astype(np.float32) * (1 - a)
                                 + arr[k].astype(np.float32) * a,
                                 0, 255).astype(np.uint8)
        elif ai_fallback is not None:
            # flow가 거의 무효(정지 배경 상시 마스크 등) → 통째 생성모델 경로
            if stats is not None:
                stats["flow_bypass"] = 1
            return ai_fallback(frames, masks, tier, chunk)
        else:
            for k in range(n):
                hm = holes[k]
                if hm is None or not hm.any():
                    continue
                out[k] = cv2.inpaint(out[k], (hm > 0).astype(np.uint8),
                                     5, cv2.INPAINT_TELEA)
    return out


# ---------------- Phase C: Effect-Aware Locator ----------------
def associated_effect_mask(frame, core_mask, band=25, diff_thr=7.0,
                           feather=3.0):
    """core(글자/카드 몸체) 주변의 부수효과(외곽선·그림자·glow·색번짐) soft mask.

    배경 추정(TELEA)과의 차이가 큰 링 영역만 확장한다. 반환 u8 (0..255 soft).
    """
    mb = (core_mask > 127).astype(np.uint8)
    if not mb.any():
        return np.zeros_like(core_mask)
    ring = cv2.dilate(mb, np.ones((band, band), np.uint8))
    bg = cv2.inpaint(frame, cv2.dilate(mb, np.ones((5, 5), np.uint8)),
                     5, cv2.INPAINT_TELEA)
    diff = np.abs(frame.astype(np.float32) - bg.astype(np.float32)).max(axis=2)
    eff = ((diff > diff_thr) & (ring > 0)).astype(np.uint8) * 255
    eff = cv2.morphologyEx(eff, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    # 아주 작은 파편 제거
    ncomp, lab, st, _ = cv2.connectedComponentsWithStats(eff, 8)
    for ci in range(1, ncomp):
        if st[ci, cv2.CC_STAT_AREA] < 12:
            eff[lab == ci] = 0
    eff = np.maximum(eff, mb * 255)
    if feather > 0:
        eff = cv2.GaussianBlur(eff, (0, 0), feather)
    return eff


# ---------------- Phase C: Preserver (절대 자물쇠) ----------------
def preserve_outside(src_frame, out_frame, allowed_mask_u8, soft=2.0):
    """allowed(=effect mask ∪ 카드 matte ∪ paste) 밖은 원본 화소로 강제 복귀."""
    a = allowed_mask_u8.astype(np.float32)
    if soft > 0:
        a = cv2.GaussianBlur(a, (0, 0), soft)
    a = np.clip(a / 255.0, 0, 1)[..., None]
    return np.clip(src_frame.astype(np.float32) * (1 - a)
                   + out_frame.astype(np.float32) * a, 0, 255).astype(np.uint8)
