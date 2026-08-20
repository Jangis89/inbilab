# -*- coding: utf-8 -*-
"""V32 골든 영상 10개 제작 (Phase 2).

구성 (명세: >=5개는 clean ground-truth + burn-in):
  g01~g05  GT 쌍: clean 배경(실사 crop 3 + 합성 2) 위에 다양한 스타일 자막 burn-in
  g06~g10  실영상(GT 없음): 기준 벤치 원본의 서로 다른 20초 구간

저장 (원본 자동삭제 정책 대비, 마스터는 videos-clips/bench-assets/golden 에 보존):
  videos-clips/bench-assets/golden/gNN_clean.mp4   (GT 쌍만)
  videos-clips/bench-assets/golden/gNN_input.mp4   (파이프라인 입력본)
  videos-source/golden/gNN.mp4                     (실행용 — 사라지면 마스터에서 복원)
프로젝트 행: beac0002-0000-4000-8000-0000000000NN (wm_mode auto, tier fast)
"""
import json, os, subprocess, sys, tempfile, time

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
MASTER_PATH = "bench-assets/benchmark_master.mp4"
GOLD_PFX = "bench-assets/golden"
SRC_PROJECT = "31118dec-b65d-4d99-b67e-61ab3333094b"
FPS = 30
DUR = 20  # 초 — GPU 비용 통제

FONT_KR = "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"
FONT_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def sbh(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 실패: {' '.join(cmd[:8])}...\n{r.stderr.decode()[-800:]}")


def obj_exists(bucket, path):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh({"Range": "bytes=0-0"}), timeout=30)
    return r.status_code in (200, 206)


def upload(bucket, path, fp):
    with open(fp, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                          headers=sbh({"Content-Type": "video/mp4", "x-upsert": "true"}),
                          data=f, timeout=1800)
    r.raise_for_status()


def download(bucket, path, fp):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh(), stream=True, timeout=1800)
    r.raise_for_status()
    with open(fp, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)


def font(kr=True):
    p = FONT_KR if kr else FONT_EN
    if not os.path.exists(p):
        p = FONT_EN if os.path.exists(FONT_EN) else ""
    return p


# ---- burn-in 스타일 5종 (위치·크기·색·테두리·박스·등장타이밍 다양화) ----
def drawtext(style, text_kr, text_en):
    f_kr, f_en = font(True), font(False)
    if style == 0:   # 하단 중앙 흰색+검은 테두리 (가장 흔한 형태)
        return (f"drawtext=fontfile={f_kr}:text='{text_kr}':fontsize=58:fontcolor=white:"
                f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-220")
    if style == 1:   # 노란색, 간헐 등장 (2~6s, 9~14s)
        return (f"drawtext=fontfile={f_kr}:text='{text_kr}':fontsize=64:fontcolor=yellow:"
                f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=h-260:"
                f"enable='between(t,2,6)+between(t,9,14)'")
    if style == 2:   # 반투명 박스 배경 (예능 자막형)
        return (f"drawtext=fontfile={f_kr}:text='{text_kr}':fontsize=52:fontcolor=white:"
                f"box=1:boxcolor=black@0.55:boxborderw=14:x=(w-text_w)/2:y=h-200")
    if style == 3:   # 중앙 큰 영어 자막 + 워터마크 겸용 코너 텍스트
        return (f"drawtext=fontfile={f_en}:text='{text_en}':fontsize=70:fontcolor=white:"
                f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=(h-text_h)/2,"
                f"drawtext=fontfile={f_en}:text='@inbilab':fontsize=36:fontcolor=white@0.8:"
                f"x=w-text_w-40:y=60")
    # style 4: 두 줄 자막, 등장/퇴장 시간 다름
    return (f"drawtext=fontfile={f_kr}:text='{text_kr}':fontsize=50:fontcolor=white:"
            f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-280:enable='between(t,1,17)',"
            f"drawtext=fontfile={f_kr}:text='두번째 줄 자막입니다':fontsize=44:fontcolor=white:"
            f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-210:enable='between(t,4,12)'")


TEXTS = [("오늘은 날씨가 정말 좋네요", "Beautiful day outside"),
         ("이 장면 진짜 대박이다", "This scene is amazing"),
         ("구독과 좋아요 부탁드려요", "Like and subscribe"),
         ("잠시 후 놀라운 일이 벌어집니다", "Something amazing happens"),
         ("여기서부터 집중해서 보세요", "Watch closely from here")]


def make_goldens(tmp):
    master = os.path.join(tmp, "master.mp4")
    print("[golden] 기준 마스터 다운로드")
    download("videos-clips", MASTER_PATH, master)
    out = []

    # clean 배경: 실사 crop 3개 (자막 밴드를 피해 중앙부 crop, 서로 다른 구간)
    for i, ss in enumerate((5, 60, 120)):
        g = f"g{i+1:02d}"
        clean = os.path.join(tmp, f"{g}_clean.mp4")
        run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(DUR), "-i", master,
             "-vf", f"crop=1080:1080:0:300,fps={FPS}", "-an",
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean, "-y"])
        burned = os.path.join(tmp, f"{g}_input.mp4")
        run(["ffmpeg", "-v", "error", "-i", clean, "-vf", drawtext(i, *TEXTS[i]),
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
        out.append((g, clean, burned, "gt-real"))

    # g04: 합성 노이즈/패턴 골든 폐기 → 실사 crop (다른 화면부, 시간축 연속)
    g = "g04"
    clean = os.path.join(tmp, "g04_clean.mp4")
    run(["ffmpeg", "-v", "error", "-ss", "150", "-t", str(DUR), "-i", master,
         "-vf", f"crop=1080:1080:0:600,fps={FPS}", "-an",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean, "-y"])
    burned = os.path.join(tmp, "g04_input.mp4")
    run(["ffmpeg", "-v", "error", "-i", clean, "-vf", drawtext(3, *TEXTS[3]),
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
    out.append(("g04", clean, burned, "gt-real"))
    g = "g05"
    clean = os.path.join(tmp, "g05_clean.mp4")
    run(["ffmpeg", "-v", "error", "-ss", "90", "-t", str(DUR), "-i", master,
         "-vf", f"crop=1080:1080:0:600,fps={FPS}", "-an",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean, "-y"])
    burned = os.path.join(tmp, "g05_input.mp4")
    run(["ffmpeg", "-v", "error", "-i", clean, "-vf", drawtext(4, *TEXTS[4]),
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
    out.append(("g05", clean, burned, "gt-real"))

    # 박스형 골든 5종 (g11~g15 = B01~B05) — 실사 crop 배경, clean GT 보유
    f_kr, f_en = font(True), font(False)
    box_styles = [
        ("g11", f"drawbox=x=0:y=ih-260:w=iw:h=120:color=black@0.6:t=fill,"
                f"drawtext=fontfile={f_en}:text='FULL WIDTH BAR SUBTITLE':fontsize=48:"
                f"fontcolor=white:x=(w-text_w)/2:y=h-230"),
        ("g12", f"drawtext=fontfile={f_kr}:text='둥근 캡션 박스 자막입니다':fontsize=50:"
                f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=18:"
                f"x=(w-text_w)/2:y=h-300"),
        ("g13", f"drawtext=fontfile={f_en}:text='VARIETY SHOW':fontsize=58:"
                f"fontcolor=black:box=1:boxcolor=0xFFD24A@0.95:boxborderw=16:"
                f"x=(w-text_w)/2:y=h-380"),
        ("g14", f"drawtext=fontfile={f_kr}:text='움직이는 박스 자막':fontsize=52:"
                f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=16:"
                f"x=(w-text_w)/2+30*sin(t):y=h-320-25*sin(t*1.3)"),
        ("g15", f"drawbox=x=0:y=ih-300:w=iw:h=160:color=0x101040@0.55:t=fill,"
                f"drawtext=fontfile={f_en}:text='LOWER THIRD GRADIENT BAR':fontsize=52:"
                f"fontcolor=white:x=60:y=h-270"),
    ]
    for bi, (g, flt) in enumerate(box_styles):
        clean = os.path.join(tmp, f"{g}_clean.mp4")
        # 배경은 원본 자막이 없는 중앙부 crop — clean이 진짜 clean이도록
        run(["ffmpeg", "-v", "error", "-ss", str(10 + bi * 30), "-t", str(DUR),
             "-i", master, "-vf", f"crop=1080:1080:0:300,fps={FPS}", "-an",
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean, "-y"])
        burned = os.path.join(tmp, f"{g}_input.mp4")
        run(["ffmpeg", "-v", "error", "-i", clean, "-vf", flt,
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
        out.append((g, clean, burned, "gt-box"))

    # 실영상 5개 (GT 없음 — 원본 자막 그대로, 서로 다른 구간)
    for i, ss in enumerate((0, 35, 70, 105, 140)):
        g = f"g{i+6:02d}"
        inp = os.path.join(tmp, f"{g}_input.mp4")
        run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(DUR), "-i", master,
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-an", inp, "-y"])
        out.append((g, None, inp, "real"))
    return out


# ---- RC2 Phase E: transient positive 5종 (g16~g20, clean GT 보유) ----
def make_transient_goldens(tmp, master):
    f_kr, f_en = font(True), font(False)
    styles = [
        # T01(g16): 10~30초 구간형 — 저대비 반투명 카드 (6~14s만 등장)
        ("g16", f"drawtext=fontfile={f_kr}:text='잠깐 나타나는 카드 자막':fontsize=52:"
                f"fontcolor=0x202020:box=1:boxcolor=white@0.55:boxborderw=22:"
                f"x=(w-text_w)/2:y=(h-text_h)/2-120:enable='between(t,6,14)'"),
        # T02(g17): 화면 중앙 짧은 반투명 텍스트 워터마크 (4~9s)
        ("g17", f"drawtext=fontfile={f_en}:text='SAMPLE MARK':fontsize=64:"
                f"fontcolor=white@0.45:borderw=2:bordercolor=black@0.35:"
                f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,4,9)'"),
        # T03(g18): 천천히 이동하는 반투명 워터마크 (3~16s)
        ("g18", f"drawtext=fontfile={f_en}:text='@channel_mark':fontsize=54:"
                f"fontcolor=white@0.5:borderw=2:bordercolor=black@0.3:"
                f"x=(w-text_w)/2+60*sin(t/3):y=(h-text_h)/2+40*sin(t/4):"
                f"enable='between(t,3,16)'"),
        # T05(g20): 간헐 재등장 반투명 카드 (2~5, 8~11, 14~17s)
        ("g20", f"drawtext=fontfile={f_kr}:text='간헐 등장 카드':fontsize=50:"
                f"fontcolor=0x101010:box=1:boxcolor=white@0.6:boxborderw=20:"
                f"x=(w-text_w)/2:y=(h-text_h)/2+60:"
                f"enable='between(t,2,5)+between(t,8,11)+between(t,14,17)'"),
    ]
    out = []
    for i, (g, flt) in enumerate(styles):
        clean = os.path.join(tmp, f"{g}_clean.mp4")
        run(["ffmpeg", "-v", "error", "-ss", str(20 + i * 25), "-t", str(DUR),
             "-i", master, "-vf", f"crop=1080:1080:0:300,fps={FPS}", "-an",
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean, "-y"])
        burned = os.path.join(tmp, f"{g}_input.mp4")
        run(["ffmpeg", "-v", "error", "-i", clean, "-vf", flt,
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
        out.append((g, clean, burned, "gt-transient"))
    # T04(g19): scene cut 전후에 걸치는 lower-third 카드 (cut=t10, 카드 6~14s)
    g = "g19"
    clean = os.path.join(tmp, "g19_clean.mp4")
    run(["ffmpeg", "-v", "error",
         "-ss", "15", "-t", "10", "-i", master,
         "-ss", "110", "-t", "10", "-i", master,
         "-filter_complex",
         f"[0:v]crop=1080:1080:0:300,fps={FPS}[a];[1:v]crop=1080:1080:0:600,fps={FPS}[b];"
         f"[a][b]concat=n=2:v=1:a=0[c]",
         "-map", "[c]", "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         clean, "-y"])
    burned = os.path.join(tmp, "g19_input.mp4")
    run(["ffmpeg", "-v", "error", "-i", clean, "-vf",
         f"drawtext=fontfile={f_kr}:text='장면 전환을 걸치는 하단 카드':fontsize=48:"
         f"fontcolor=white:box=1:boxcolor=0x123B70@0.6:boxborderw=18:"
         f"x=(w-text_w)/2:y=h-260:enable='between(t,6,14)'",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
    out.append(("g19", clean, burned, "gt-transient"))
    return out


# ---- RC2 Phase E: negative 5종 (g21~g25 — 실물 텍스트/사각형, 제거 0 요구) ----
def _still_pan(tmp, master, g, ss, draw, pan_x="min(iw-1080\\,t*36)", pan_y="300",
               pre_scale="scale=2160:-2"):
    """마스터의 한 프레임을 큰 캔버스로 → 텍스트/도형을 '장면의 일부'로 구운 뒤
    카메라 팬처럼 crop 창을 움직여 영상화 — 실물(장면 부착) 텍스트를 모사."""
    still = os.path.join(tmp, f"{g}_still.png")
    run(["ffmpeg", "-v", "error", "-ss", str(ss), "-i", master, "-frames:v", "1",
         "-vf", pre_scale + ("," + draw if draw else ""), still, "-y"])
    outp = os.path.join(tmp, f"{g}_input.mp4")
    run(["ffmpeg", "-v", "error", "-loop", "1", "-i", still, "-t", str(DUR),
         "-vf", f"crop=1080:1080:{pan_x}:{pan_y},fps={FPS}",
         "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium", outp, "-y"])
    return outp


def make_negative_goldens(tmp, master):
    f_kr, f_en = font(True), font(False)
    out = []
    # N01(g21): 장면 속 표지판 — 팬과 함께 움직이는 큰 글자
    d = (f"drawbox=x=760:y=520:w=560:h=260:color=0x1B5E20@1:t=fill,"
         f"drawtext=fontfile={f_kr}:text='주차금지':fontsize=96:fontcolor=white:"
         f"x=800:y=560,drawtext=fontfile={f_en}:text='NO PARKING':fontsize=44:"
         f"fontcolor=white:x=800:y=690")
    out.append(("g21", None, _still_pan(tmp, master, "g21", 40, d), "negative"))
    # N02(g22): 모니터 화면 — 사각 베젤+텍스트, 느린 팬
    d = (f"drawbox=x=700:y=400:w=760:h=500:color=0x111111@1:t=fill,"
         f"drawbox=x=730:y=430:w=700:h=440:color=0x2266AA@1:t=fill,"
         f"drawtext=fontfile={f_en}:text='SYSTEM MONITOR':fontsize=52:fontcolor=white:"
         f"x=760:y=470,drawtext=fontfile={f_en}:text='CPU 43  MEM 71':fontsize=40:"
         f"fontcolor=0xCCEEFF:x=760:y=560")
    out.append(("g22", None, _still_pan(tmp, master, "g22", 75, d,
                                        pan_x="min(iw-1080\\,t*22)"), "negative"))
    # N03(g23): 창문·문틀 — 글자 없는 강한 사각형들
    d = ("drawbox=x=650:y=250:w=500:h=700:color=0x3E2B1F@1:t=24,"
         "drawbox=x=700:y=300:w=190:h=280:color=0x87CEEB@1:t=fill,"
         "drawbox=x=910:y=300:w=190:h=280:color=0x9AD1E8@1:t=fill,"
         "drawbox=x=700:y=620:w=190:h=280:color=0x7FB8D6@1:t=fill,"
         "drawbox=x=910:y=620:w=190:h=280:color=0x8FC4DE@1:t=fill")
    out.append(("g23", None, _still_pan(tmp, master, "g23", 100, d,
                                        pan_x="min(iw-1080\\,t*30)"), "negative"))
    # N04(g24): 옷/물체에 인쇄된 글자 — 완만한 상하 흔들림 포함
    d = (f"drawtext=fontfile={f_en}:text='SPORTS CLUB 88':fontsize=58:"
         f"fontcolor=0xEEEEEE:borderw=1:bordercolor=0x555555:x=820:y=760")
    out.append(("g24", None, _still_pan(tmp, master, "g24", 130, d,
                                        pan_x="min(iw-1080\\,t*26)",
                                        pan_y="280+20*sin(t*1.7)"), "negative"))
    # N05(g25): 고정카메라 정적 장면 — 오버레이 없음, 미세 드리프트만
    out.append(("g25", None, _still_pan(tmp, master, "g25", 55, "",
                                        pan_x="8*sin(t/5)+40", pan_y="300"), "negative"))
    return out


# ---- RC3 Phase H: restoration 품질 골든 10종 (g26~g35, 전부 clean GT 보유) ----
# 목적: "글자가 지워졌는가"가 아니라 "지운 자리가 자연스러운가"를 재는 세트.
# 배경 질감·그래픽 선·얼굴·반복 패턴 등 복원이 어려운 배경 위에 오버레이를 굽는다.
def make_restoration_goldens(tmp, master):
    f_kr, f_en = font(True), font(False)
    out = []

    def _mk(g, ss, crop_y, clean_extra, burn_flt):
        clean = os.path.join(tmp, f"{g}_clean.mp4")
        vf = f"crop=1080:1080:0:{crop_y},fps={FPS}"
        if clean_extra:
            vf += "," + clean_extra
        run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(DUR), "-i", master,
             "-vf", vf, "-an", "-c:v", "libx264", "-crf", "16",
             "-preset", "medium", clean, "-y"])
        burned = os.path.join(tmp, f"{g}_input.mp4")
        run(["ffmpeg", "-v", "error", "-i", clean, "-vf", burn_flt,
             "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
        out.append((g, clean, burned, "gt-restore"))

    # R26(g26): 반투명 흰 카드 + 복잡한 질감 배경 (UAT-02 카드 재현, 상시)
    _mk("g26", 100, 600, None,
        f"drawtext=fontfile={f_kr}:text='안내 카드 문구입니다':fontsize=54:"
        f"fontcolor=0x1a1a1a:box=1:boxcolor=white@0.5:boxborderw=26:"
        f"x=(w-text_w)/2:y=(h-text_h)/2")
    # R27(g27): 그림자 있는 카드 (박스 2중 — 그림자 근사) + gradient성 배경
    _mk("g27", 55, 300, None,
        f"drawbox=x=(iw-620)/2+10:y=(ih-190)/2+10:w=620:h=190:color=black@0.35:t=fill,"
        f"drawbox=x=(iw-620)/2:y=(ih-190)/2:w=620:h=190:color=white@0.6:t=fill,"
        f"drawtext=fontfile={f_kr}:text='그림자 카드 자막':fontsize=52:"
        f"fontcolor=0x202020:x=(w-text_w)/2:y=(h-text_h)/2")
    # R28(g28): 가는 그래픽 선(격자=보존 대상) 위 자막
    _mk("g28", 15, 300, "drawgrid=w=54:h=54:t=1:color=white@0.75",
        f"drawtext=fontfile={f_kr}:text='격자 위의 자막 복원':fontsize=52:"
        f"fontcolor=white:borderw=3:bordercolor=black@0.7:"
        f"x=(w-text_w)/2:y=h-280")
    # R29(g29): 인물(얼굴·손) 구간 위 자막 — 마스터 인물 장면 사용
    _mk("g29", 128, 300, None,
        f"drawtext=fontfile={f_kr}:text='인물 위 자막 복원 검사':fontsize=52:"
        f"fontcolor=white:borderw=3:bordercolor=black@0.7:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+80")
    # R30(g30): 미세 질감(노이즈 강화) 배경 — 뭉갬이 즉시 티나는 조건
    _mk("g30", 70, 600, "noise=alls=8:allf=t+u",
        f"drawtext=fontfile={f_kr}:text='질감 배경 자막':fontsize=54:"
        f"fontcolor=white:borderw=3:bordercolor=black@0.75:"
        f"x=(w-text_w)/2:y=h-300")
    # R31(g31): 반복 패턴(촘촘한 격자) — 구조 연속성 검사
    _mk("g31", 40, 300, "drawgrid=w=24:h=24:t=1:color=0xC8C8C8@0.8",
        f"drawtext=fontfile={f_en}:text='PATTERN RESTORE TEST':fontsize=50:"
        f"fontcolor=black:box=1:boxcolor=white@0.85:boxborderw=12:"
        f"x=(w-text_w)/2:y=(h-text_h)/2")
    # R32(g32): 밝은 배경 + 저대비 흰 글자 (box 없음)
    _mk("g32", 5, 100, "eq=brightness=0.12",
        f"drawtext=fontfile={f_en}:text='LOW CONTRAST WHITE':fontsize=56:"
        f"fontcolor=white@0.9:x=(w-text_w)/2:y=h-320")
    # R33(g33): 두꺼운 외곽선+그림자 글자 — 계층 마스크 검사
    _mk("g33", 90, 300, None,
        f"drawtext=fontfile={f_kr}:text='두꺼운 외곽선 자막':fontsize=60:"
        f"fontcolor=0xFFE24A:borderw=8:bordercolor=black:shadowx=6:shadowy=6:"
        f"shadowcolor=black@0.7:x=(w-text_w)/2:y=h-300")
    # R34(g34): 카메라 팬 + transient 반투명 카드 (4~12s)
    still = os.path.join(tmp, "g34_still.png")
    run(["ffmpeg", "-v", "error", "-ss", "45", "-i", master, "-frames:v", "1",
         "-vf", "scale=2160:-2", still, "-y"])
    clean34 = os.path.join(tmp, "g34_clean.mp4")
    run(["ffmpeg", "-v", "error", "-loop", "1", "-i", still, "-t", str(DUR),
         "-vf", f"crop=1080:1080:min(iw-1080\,t*40):200,fps={FPS}", "-an",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", clean34, "-y"])
    burned34 = os.path.join(tmp, "g34_input.mp4")
    run(["ffmpeg", "-v", "error", "-i", clean34, "-vf",
         f"drawtext=fontfile={f_kr}:text='이동 중 카드 등장':fontsize=52:"
         f"fontcolor=0x202020:box=1:boxcolor=white@0.55:boxborderw=22:"
         f"x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,4,12)'",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned34, "-y"])
    out.append(("g34", clean34, burned34, "gt-restore"))
    # R35(g35): 움직이는 그래픽 바(보존 대상) 옆 자막
    clean35 = os.path.join(tmp, "g35_clean.mp4")
    run(["ffmpeg", "-v", "error", "-ss", "110", "-t", str(DUR), "-i", master,
         "-filter_complex",
         "[0:v]crop=1080:1080:0:300,fps=30[bg];"
         "color=c=0x2A6BD4:s=420x36[bar];"
         "[bg][bar]overlay=x=80+140*sin(t/2):y=760",
         "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         clean35, "-y"])
    burned35 = os.path.join(tmp, "g35_input.mp4")
    run(["ffmpeg", "-v", "error", "-i", clean35, "-vf",
         f"drawtext=fontfile={f_kr}:text='움직이는 그래픽 옆 자막':fontsize=50:"
         f"fontcolor=white:borderw=3:bordercolor=black@0.7:"
         f"x=(w-text_w)/2:y=860",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned35, "-y"])
    out.append(("g35", clean35, burned35, "gt-restore"))
    return out


def ensure_projects():
    src = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{SRC_PROJECT}", "select": "user_id"},
                       headers=sbh(), timeout=30)
    src.raise_for_status()
    uid = src.json()[0]["user_id"]
    for i in range(1, 36):
        pid = f"beac0002-0000-4000-8000-0000000000{i:02d}"
        g = f"g{i:02d}"
        r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                         params={"id": f"eq.{pid}", "select": "id"}, headers=sbh(), timeout=30)
        r.raise_for_status()
        if r.json():
            continue
        ins = {"id": pid, "user_id": uid, "title": f"[v32-golden] {g}",
               "source_path": f"golden/{g}.mp4", "objective": "wm_remove",
               "status": "wm_queued", "status_detail": "v32 golden",
               "wm_mode": "auto", "wm_tier": "fast"}
        r2 = requests.post(f"{SB_URL}/rest/v1/sc_projects",
                           headers=sbh({"Content-Type": "application/json",
                                        "Prefer": "return=minimal"}),
                           data=json.dumps(ins), timeout=30)
        r2.raise_for_status()
    print("[golden] 프로젝트 행 15개 확인")


def main():
    tmp = tempfile.mkdtemp(prefix="golden-")
    # 기존 manifest 재사용 — 이미 있는 골든(g01~g15)은 절대 다시 만들지 않는다
    # (기존 15/15 기준선 보존). 새 항목(g16~g25)만 추가 제작.
    try:
        prev = requests.get(f"{SB_URL}/storage/v1/object/videos-clips/{GOLD_PFX}/manifest.json",
                            headers=sbh(), timeout=60).json()
        have = {m["g"] for m in prev}
    except Exception:
        prev, have = [], set()
    made = []
    if not have:
        made += make_goldens(tmp)
    master = os.path.join(tmp, "master.mp4")
    if not os.path.exists(master):
        download("videos-clips", MASTER_PATH, master)
    made += [m for m in make_transient_goldens(tmp, master) if m[0] not in have]
    made += [m for m in make_negative_goldens(tmp, master) if m[0] not in have]
    made += [m for m in make_restoration_goldens(tmp, master) if m[0] not in have]
    manifest = list(prev)
    for g, clean, inp, kind in made:
        if clean:
            upload("videos-clips", f"{GOLD_PFX}/{g}_clean.mp4", clean)
        upload("videos-clips", f"{GOLD_PFX}/{g}_input.mp4", inp)
        upload("videos-source", f"golden/{g}.mp4", inp)
        manifest.append({"g": g, "kind": kind, "has_gt": bool(clean),
                         "bytes": os.path.getsize(inp)})
        print(f"[golden] {g} ({kind}) 업로드 완료 {os.path.getsize(inp)/1e6:.1f}MB")
    manifest.sort(key=lambda m: m["g"])
    ensure_projects()
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    requests.post(f"{SB_URL}/storage/v1/object/videos-clips/{GOLD_PFX}/manifest.json",
                  headers=sbh({"Content-Type": "application/json", "x-upsert": "true"}),
                  data=json.dumps(manifest).encode(), timeout=60).raise_for_status()
    json.dump(manifest, open("GOLDEN_MANIFEST.json", "w"), ensure_ascii=False, indent=1)
    print("[golden] 완료 —", len(manifest), "개")


if __name__ == "__main__":
    main()
