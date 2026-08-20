#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RC3 복원 품질 평가기 (Phase C)
- "글자가 지워졌는가"가 아니라 "지운 자리가 자연스러운가"를 잰다.
- GT가 있으면(합성 골든): ROI PSNR/SSIM/(가능 시 LPIPS), 고주파 보존율, 경계 seam.
- GT가 없으면(실제 UAT): pseudo-reference 지표 —
    stain(질감 뭉갬) = 복원영역 선명도비 + 색 cast
    card residual   = 카드 경계 edge 에너지 + 내부 lift
    temporal flicker = optical-flow 정렬 후 warp error (src 대비 배율)
    residual glyph  = 지운 영역 내 잔존 글자 획 검출
- 사용: evaluate_restoration_quality.py --src A.mp4 --out B.mp4 [--ref CLEAN.mp4]
        [--card x0,y0,x1,y1,rad] [--fps 1] [--csv out.csv] [--tag name]
  좌표는 원본 해상도 픽셀. --card 미지정 시 card 지표 생략.
주의: 자동지표는 개선방향/회귀탐지용. 대표 육안 1× 재생 판정이 최종 gate다 (스펙 C.4/C.5).
"""
import argparse, csv, json, subprocess, sys, tempfile, os
import numpy as np
import cv2

def _frames(path, fps, scale_w=540):
    d = tempfile.mkdtemp(prefix="rq_")
    subprocess.run(["ffmpeg","-loglevel","error","-i",path,
                    "-vf",f"fps={fps},scale={scale_w}:-2","-start_number","0",
                    os.path.join(d,"%05d.png")], check=True)
    fs = sorted(os.listdir(d))
    return d, len(fs)

def _rd(d,i): return cv2.imread(os.path.join(d,f"{i:05d}.png"))

def _gray(a): return cv2.cvtColor(a,cv2.COLOR_BGR2GRAY).astype(np.float32)

def _diff_mask(s,r,thr=40):
    d=cv2.absdiff(s,r).max(axis=2)
    m=(d>thr).astype(np.uint8)
    return cv2.morphologyEx(m,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))

def stain_frame(s,r):
    """복원영역 질감 뭉갬: 선명도비(1=동등)와 색 cast. 반환 None=복원영역 미미."""
    m=_diff_mask(s,r)
    if int(m.sum())<400: return None
    m=cv2.dilate(m,np.ones((5,5),np.uint8))
    ring=cv2.dilate(m,np.ones((21,21),np.uint8))-m
    g=_gray(r); lap=cv2.Laplacian(g,cv2.CV_32F)
    sh_in=float(np.abs(lap[m>0]).mean()); sh_out=float(np.abs(lap[ring>0]).mean())
    sharp=sh_in/max(sh_out,1e-3)
    cast=float(np.abs(r[m>0].mean(axis=0)-r[ring>0].mean(axis=0)).max())
    return sharp, cast

def card_frame(img, rect, rad):
    x0,y0,x1,y1=rect
    g=_gray(img)
    gx=cv2.Sobel(g,cv2.CV_32F,1,0); gy=cv2.Sobel(g,cv2.CV_32F,0,1)
    mag=np.sqrt(gx*gx+gy*gy)
    band=np.zeros(g.shape,bool)
    band[max(0,y0-3):y0+4,x0+rad:x1-rad]=True; band[y1-3:y1+4,x0+rad:x1-rad]=True
    band[y0+rad:y1-rad,max(0,x0-3):x0+4]=True; band[y0+rad:y1-rad,x1-3:x1+4]=True
    edge=float(mag[band].mean())
    inm=np.zeros(g.shape,bool); inm[y0+10:y1-10,x0+10:x1-10]=True
    ring=np.zeros(g.shape,bool); ring[max(0,y0-30):y1+30,max(0,x0-30):x1+30]=True; ring&=~inm
    lift=float(g[inm].mean()-g[ring].mean())
    return edge,lift

def glyph_residual(s,r):
    """지운 영역(m) 안에 남은 어두운 획 성분 픽셀수 (0이 목표)."""
    m=_diff_mask(s,r)
    if int(m.sum())<400: return 0
    m=cv2.dilate(m,np.ones((7,7),np.uint8))
    g=_gray(r).astype(np.uint8)
    med=cv2.medianBlur(g,21).astype(np.float32)
    stroke=((med-g.astype(np.float32))>30)&(m>0)
    stroke=cv2.morphologyEx(stroke.astype(np.uint8),cv2.MORPH_OPEN,np.ones((2,2),np.uint8))
    return int(stroke.sum())

def flicker(ds,dr,n,step=1):
    """flow 정렬 warp error: out에서의 오차 / src에서의 오차 비율(>1=출력이 더 흔들림)."""
    rats=[]
    for i in range(0,n-step-1,max(1,n//40)):
        s0,s1=_rd(ds,i),_rd(ds,i+step); r0,r1=_rd(dr,i),_rd(dr,i+step)
        if s0 is None or s1 is None or r0 is None or r1 is None: continue
        m=_diff_mask(s0,r0)
        if int(m.sum())<400: continue
        m=cv2.dilate(m,np.ones((9,9),np.uint8))>0
        g0,g1=_gray(s0),_gray(s1)
        fl=cv2.calcOpticalFlowFarneback(g0,g1,None,0.5,3,21,3,5,1.2,0)
        h,w=g0.shape
        gy,gx=np.mgrid[0:h,0:w].astype(np.float32)
        mx=(gx+fl[...,0]); my=(gy+fl[...,1])
        ws=cv2.remap(s1,mx,my,cv2.INTER_LINEAR)
        wr=cv2.remap(r1,mx,my,cv2.INTER_LINEAR)
        es=float(cv2.absdiff(ws,s0).max(axis=2)[m].mean())
        er=float(cv2.absdiff(wr,r0).max(axis=2)[m].mean())
        if es>1e-3: rats.append(er/es)
    return float(np.median(rats)) if rats else None

def roi_psnr_ssim(ref,out,m):
    if int(m.sum())<50: return None,None
    mm=m>0
    d=(ref.astype(np.float32)-out.astype(np.float32))
    mse=float((d[mm]**2).mean())
    psnr=99.0 if mse<1e-6 else 10*np.log10(255*255/mse)
    try:
        from skimage.metrics import structural_similarity as ssim
        g1=_gray(ref); g2=_gray(out)
        _,smap=ssim(g1,g2,data_range=255,full=True)
        return psnr,float(smap[mm].mean())
    except Exception:
        return psnr,None

def highfreq_ratio(ref,out,m):
    if int(m.sum())<50: return None
    mm=m>0
    k=lambda a:np.abs(cv2.Laplacian(_gray(a),cv2.CV_32F))
    hr=float(k(out)[mm].mean())/max(float(k(ref)[mm].mean()),1e-3)
    return hr

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--src",required=True); ap.add_argument("--out",required=True)
    ap.add_argument("--ref"); ap.add_argument("--card"); ap.add_argument("--fps",type=float,default=1)
    ap.add_argument("--csv"); ap.add_argument("--tag",default="clip")
    a=ap.parse_args()
    ds,ns=_frames(a.src,a.fps); dr,nr=_frames(a.out,a.fps); n=min(ns,nr)
    dref=None
    if a.ref: dref,_=_frames(a.ref,a.fps)
    card=None
    if a.card:
        v=[int(x) for x in a.card.split(",")]; card=(v[:4],v[4] if len(v)>4 else 27)

    sharps,casts,glyphs,edges,lifts=[],[],[],[],[]
    psnrs,ssims,hfrs=[],[],[]
    for i in range(n):
        s=_rd(ds,i); r=_rd(dr,i)
        if s is None or r is None: continue
        st=stain_frame(s,r)
        if st: sharps.append(st[0]); casts.append(st[1])
        glyphs.append(glyph_residual(s,r))
        if card:
            e,l=card_frame(r,card[0],card[1]); edges.append(e); lifts.append(l)
        if dref is not None:
            ref=_rd(dref,i)
            if ref is not None:
                m=cv2.dilate(_diff_mask(s,ref,30),np.ones((7,7),np.uint8))
                p,ss=roi_psnr_ssim(ref,r,m)
                if p is not None: psnrs.append(p)
                if ss is not None: ssims.append(ss)
                h=highfreq_ratio(ref,r,m)
                if h is not None: hfrs.append(h)
    fl=flicker(ds,dr,n)
    def q(v,p): return round(float(np.percentile(v,p)),3) if v else None
    sharps_arr=np.array(sharps) if sharps else np.array([1.0])
    stain_comp=round(float(np.mean(np.clip(1-sharps_arr,0,1))+ (np.mean(casts)/100 if casts else 0)),4)
    res={"tag":a.tag,"frames":n,
         "sharp_p50":q(sharps,50),"sharp_p10":q(sharps,10),
         "stain_frames_lt06":int((sharps_arr<0.6).sum()),
         "cast_p90":q(casts,90),
         "stain_composite":stain_comp,
         "glyph_resid_p90":q(glyphs,90),
         "card_edge_p50":q(edges,50),"card_edge_p90":q(edges,90),
         "card_lift_p50":q(lifts,50),"card_absl_p90":q([abs(x) for x in lifts],90) if lifts else None,
         "card_residual_score":(round(float(np.median(edges)+np.median(np.abs(lifts))),3) if edges else None),
         "flicker_ratio":fl,
         "roi_psnr_p50":q(psnrs,50),"roi_ssim_p50":q(ssims,50),"hf_ratio_p50":q(hfrs,50)}
    print(json.dumps(res,ensure_ascii=False,indent=1))
    if a.csv:
        new=not os.path.exists(a.csv)
        with open(a.csv,"a",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(res.keys()))
            if new: w.writeheader()
            w.writerow(res)

if __name__=="__main__":
    main()
