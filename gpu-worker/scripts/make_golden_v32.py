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


def ensure_projects():
    src = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{SRC_PROJECT}", "select": "user_id"},
                       headers=sbh(), timeout=30)
    src.raise_for_status()
    uid = src.json()[0]["user_id"]
    for i in range(1, 16):
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
    made = make_goldens(tmp)
    manifest = []
    for g, clean, inp, kind in made:
        if clean:
            upload("videos-clips", f"{GOLD_PFX}/{g}_clean.mp4", clean)
        upload("videos-clips", f"{GOLD_PFX}/{g}_input.mp4", inp)
        upload("videos-source", f"golden/{g}.mp4", inp)
        manifest.append({"g": g, "kind": kind, "has_gt": bool(clean),
                         "bytes": os.path.getsize(inp)})
        print(f"[golden] {g} ({kind}) 업로드 완료 {os.path.getsize(inp)/1e6:.1f}MB")
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
