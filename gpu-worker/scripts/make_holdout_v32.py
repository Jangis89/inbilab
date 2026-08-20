# -*- coding: utf-8 -*-
"""V32 RC4 blind holdout 20종 제작 (Phase B — 개발 전 동결).

명세(REV2 §B): 사람/동물 5 + 카드/그림자 5 + 반복패턴 5 + 카메라이동/컷 5.
전부 clean GT 쌍 보유(권리 허용 마스터 benchmark_master.mp4에서 파생).
골든(g01~g35)과 겹치지 않는 새 스타일·새 구간만 사용한다.

동결 규칙:
 - 이 스크립트는 개발 시작 전 1회 실행되어 SHA256 manifest를 산출한다.
 - 이후 개발 중에는 holdout에 파이프라인을 돌려 threshold를 조정하지 않는다.
   (실행은 Phase L 채점 시점에만)

저장:
  videos-clips/bench-assets/holdout/hNN_clean.mp4
  videos-clips/bench-assets/holdout/hNN_input.mp4
  videos-source/holdout/hNN.mp4          (실행용 입력)
프로젝트 행: beac0007-0000-4000-8000-0000000000NN (wm_mode auto, tier fast)
산출: HOLDOUT_MANIFEST.csv (id,type,desc,ss,sha256_input,sha256_clean,bytes)
"""
import csv
import hashlib
import json
import os
import subprocess
import tempfile

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
MASTER_PATH = "bench-assets/benchmark_master.mp4"
HOLD_PFX = "bench-assets/holdout"
SRC_PROJECT = "31118dec-b65d-4d99-b67e-61ab3333094b"
FPS = 30
DUR = 20

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
        raise RuntimeError(f"ffmpeg 실패: {' '.join(str(c) for c in cmd[:8])}...\n"
                           f"{r.stderr.decode()[-800:]}")


def upload(bucket, path, fp):
    with open(fp, "rb") as f:
        r = requests.post(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                          headers=sbh({"Content-Type": "video/mp4",
                                       "x-upsert": "true"}),
                          data=f, timeout=1800)
    r.raise_for_status()


def download(bucket, path, fp):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh(), stream=True, timeout=1800)
    r.raise_for_status()
    with open(fp, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch)


def sha256(fp):
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def font(kr=True):
    p = FONT_KR if kr else FONT_EN
    if not os.path.exists(p):
        p = FONT_EN if os.path.exists(FONT_EN) else ""
    return p


def _clip(tmp, master, name, ss, crop_y, clean_extra=None, complex_clean=None):
    """마스터에서 clean 배경 클립 생성."""
    clean = os.path.join(tmp, f"{name}_clean.mp4")
    if complex_clean:
        complex_clean(clean)
        return clean
    vf = f"crop=1080:1080:0:{crop_y},fps={FPS}"
    if clean_extra:
        vf += "," + clean_extra
    run(["ffmpeg", "-v", "error", "-ss", str(ss), "-t", str(DUR), "-i", master,
         "-vf", vf, "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         clean, "-y"])
    return clean


def _burn(tmp, name, clean, flt, complex_burn=None):
    burned = os.path.join(tmp, f"{name}_input.mp4")
    if complex_burn:
        complex_burn(clean, burned)
        return burned
    run(["ffmpeg", "-v", "error", "-i", clean, "-vf", flt,
         "-c:v", "libx264", "-crf", "16", "-preset", "medium", burned, "-y"])
    return burned


def build(tmp, master):
    f_kr, f_en = font(True), font(False)
    out = []  # (id, type, desc, ss, clean, input)

    def mk(hid, typ, desc, ss, crop_y, burn_flt, clean_extra=None):
        c = _clip(tmp, master, hid, ss, crop_y, clean_extra)
        b = _burn(tmp, hid, c, burn_flt)
        out.append((hid, typ, desc, ss, c, b))

    # ---------- H01~H05: 사람/손/다리/동물 위 오버레이 ----------
    # (마스터 인물 구간 120~150s 대역 + 다양한 신체 겹침 스타일)
    mk("h01", "person", "인물 하반신 위 2줄 자막", 122, 500,
       f"drawtext=fontfile={f_kr}:text='다리를 가리는 자막 첫줄':fontsize=54:"
       f"fontcolor=white:borderw=3:bordercolor=black@0.75:x=(w-text_w)/2:y=h-360,"
       f"drawtext=fontfile={f_kr}:text='둘째 줄은 더 아래':fontsize=48:"
       f"fontcolor=white:borderw=3:bordercolor=black@0.75:x=(w-text_w)/2:y=h-290")
    mk("h02", "person", "인물 중앙 반투명 스티커형 박스", 131, 300,
       f"drawtext=fontfile={f_kr}:text='몸통 겹침 카드':fontsize=56:"
       f"fontcolor=0x111111:box=1:boxcolor=0xF2F2F2@0.62:boxborderw=24:"
       f"x=(w-text_w)/2:y=(h-text_h)/2+40")
    mk("h03", "person", "인물 위 노란 예능자막(간헐 3구간)", 137, 400,
       f"drawtext=fontfile={f_kr}:text='순간 등장 예능 자막':fontsize=62:"
       f"fontcolor=0xFFE24A:borderw=5:bordercolor=black:shadowx=4:shadowy=4:"
       f"shadowcolor=black@0.6:x=(w-text_w)/2:y=(h-text_h)/2+120:"
       f"enable='between(t,1,4)+between(t,7,11)+between(t,15,18)'")
    mk("h04", "person", "인물 팔·손 대역 백색 워터마크", 144, 300,
       f"drawtext=fontfile={f_en}:text='HOLDOUT MARK':fontsize=58:"
       f"fontcolor=white@0.5:borderw=2:bordercolor=black@0.3:"
       f"x=(w-text_w)/2-120:y=(h-text_h)/2-60")
    mk("h05", "person", "인물 어깨선 걸친 lower-third 바", 126, 200,
       f"drawbox=x=0:y=ih-420:w=iw:h=130:color=0x0E2A52@0.65:t=fill,"
       f"drawtext=fontfile={f_kr}:text='어깨선을 걸치는 하단 바':fontsize=46:"
       f"fontcolor=white:x=70:y=h-390")

    # ---------- H06~H10: 반투명 카드/그림자/gradient ----------
    mk("h06", "card", "저알파(0.38) 대형 카드 상시", 12, 600,
       f"drawtext=fontfile={f_kr}:text='저알파 대형 카드 문구':fontsize=58:"
       f"fontcolor=0x161616:box=1:boxcolor=white@0.38:boxborderw=34:"
       f"x=(w-text_w)/2:y=(h-text_h)/2-40")
    mk("h07", "card", "고알파(0.72) 어두운 카드 + 흰 글자", 47, 300,
       f"drawtext=fontfile={f_kr}:text='어두운 반투명 카드':fontsize=54:"
       f"fontcolor=white:box=1:boxcolor=0x101018@0.72:boxborderw=28:"
       f"x=(w-text_w)/2:y=(h-text_h)/2+60")
    mk("h08", "card", "그림자+테두리 이중 카드(6~16s)", 83, 300,
       f"drawbox=x=(iw-680)/2+14:y=(ih-220)/2+14:w=680:h=220:color=black@0.4:t=fill:"
       f"enable='between(t,6,16)',"
       f"drawbox=x=(iw-680)/2:y=(ih-220)/2:w=680:h=220:color=0xF6F6F0@0.58:t=fill:"
       f"enable='between(t,6,16)',"
       f"drawbox=x=(iw-680)/2:y=(ih-220)/2:w=680:h=220:color=0x333333@0.9:t=3:"
       f"enable='between(t,6,16)',"
       f"drawtext=fontfile={f_kr}:text='그림자 이중 카드':fontsize=52:"
       f"fontcolor=0x1d1d1d:x=(w-text_w)/2:y=(h-text_h)/2:enable='between(t,6,16)'")
    mk("h09", "card", "세로 gradient 배경 위 밝은 카드", 66, 100,
       f"drawtext=fontfile={f_kr}:text='그라데이션 위 카드':fontsize=52:"
       f"fontcolor=0x202020:box=1:boxcolor=0xFFFDF5@0.55:boxborderw=26:"
       f"x=(w-text_w)/2:y=(h-text_h)/2",
       clean_extra="gradfun=strength=0.7,eq=brightness=0.06")
    mk("h10", "card", "카드 2장 동시(상단 작은+중앙 큰)", 30, 300,
       f"drawtext=fontfile={f_en}:text='TOP TAG':fontsize=40:fontcolor=0x151515:"
       f"box=1:boxcolor=0xFFD9A0@0.66:boxborderw=14:x=90:y=140,"
       f"drawtext=fontfile={f_kr}:text='중앙 반투명 안내 카드':fontsize=54:"
       f"fontcolor=0x101010:box=1:boxcolor=white@0.52:boxborderw=28:"
       f"x=(w-text_w)/2:y=(h-text_h)/2+30")

    # ---------- H11~H15: 격자/기와/반복무늬/수풀/자갈 질감 ----------
    mk("h11", "pattern", "촘촘 벽돌(offset 격자) 위 자막", 20, 300,
       f"drawtext=fontfile={f_kr}:text='벽돌 무늬 위 자막':fontsize=54:"
       f"fontcolor=white:borderw=3:bordercolor=black@0.7:x=(w-text_w)/2:y=(h-text_h)/2",
       clean_extra=("drawgrid=w=88:h=34:t=2:color=0xB98A5A@0.85,"
                    "drawgrid=x=44:w=88:h=68:t=2:color=0xB98A5A@0.5"))
    mk("h12", "pattern", "대각 사선 스트라이프 위 박스자막", 58, 600,
       f"drawtext=fontfile={f_en}:text='DIAGONAL PATTERN':fontsize=50:"
       f"fontcolor=black:box=1:boxcolor=white@0.8:boxborderw=16:"
       f"x=(w-text_w)/2:y=(h-text_h)/2",
       clean_extra="drawgrid=w=36:h=36:t=1:color=0xD8D8D8@0.7,rotate=0.12:c=none")
    mk("h13", "pattern", "자갈성 노이즈+미세격자 질감", 74, 600,
       f"drawtext=fontfile={f_kr}:text='자갈 질감 위 자막':fontsize=56:"
       f"fontcolor=white:borderw=4:bordercolor=black@0.8:x=(w-text_w)/2:y=h-320",
       clean_extra="noise=alls=9:allf=t+u,unsharp=5:5:0.6")
    mk("h14", "pattern", "수풀(고주파 실사) 구간 위 자막", 148, 300,
       f"drawtext=fontfile={f_kr}:text='수풀 배경 복원 검사':fontsize=52:"
       f"fontcolor=white:borderw=3:bordercolor=black@0.7:"
       f"x=(w-text_w)/2:y=(h-text_h)/2+100")
    mk("h15", "pattern", "기와성 반복 아치(이중 곡선 격자)", 100, 300,
       f"drawtext=fontfile={f_en}:text='ROOF TILE RESTORE':fontsize=50:"
       f"fontcolor=0x101010:box=1:boxcolor=0xEDEDED@0.75:boxborderw=14:"
       f"x=(w-text_w)/2:y=(h-text_h)/2-40",
       clean_extra=("drawgrid=w=64:h=28:t=2:color=0x6E6E6E@0.8,"
                    "drawgrid=x=32:y=14:w=64:h=28:t=1:color=0x9A9A9A@0.6"))

    # ---------- H16~H20: 카메라 이동/scene cut/이동 overlay ----------
    # h16: still-pan(수평 팬) + 상시 하단 자막
    def _pan_clean(dst, ss, speed, crop_y="300", zoom="scale=2160:-2"):
        still = dst.replace("_clean.mp4", "_still.png")
        run(["ffmpeg", "-v", "error", "-ss", str(ss), "-i", master, "-frames:v", "1",
             "-vf", zoom, still, "-y"])
        run(["ffmpeg", "-v", "error", "-loop", "1", "-i", still, "-t", str(DUR),
             "-vf", f"crop=1080:1080:min(iw-1080\\,t*{speed}):{crop_y},fps={FPS}",
             "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium", dst, "-y"])

    c16 = os.path.join(tmp, "h16_clean.mp4")
    _pan_clean(c16, 35, 48)
    b16 = _burn(tmp, "h16", c16,
                f"drawtext=fontfile={f_kr}:text='팬 중 상시 하단 자막':fontsize=52:"
                f"fontcolor=white:borderw=3:bordercolor=black@0.75:"
                f"x=(w-text_w)/2:y=h-260")
    out.append(("h16", "camera", "수평 팬 + 상시 하단 자막", 35, c16, b16))

    # h17: 팬 + 반투명 카드(5~13s) — UAT-02+g34와 다른 속도/카드 스타일
    c17 = os.path.join(tmp, "h17_clean.mp4")
    _pan_clean(c17, 88, 30, crop_y="min(ih-1080\\,150+t*12)")
    b17 = _burn(tmp, "h17", c17,
                f"drawtext=fontfile={f_kr}:text='대각 팬 중 카드':fontsize=54:"
                f"fontcolor=0x151515:box=1:boxcolor=0xF4EFE2@0.6:boxborderw=30:"
                f"x=(w-text_w)/2:y=(h-text_h)/2-80:enable='between(t,5,13)'")
    out.append(("h17", "camera", "대각 팬 + 카드(5~13s)", 88, c17, b17))

    # h18: scene cut 2회(3분할 concat) 걸친 상시 워터마크
    c18 = os.path.join(tmp, "h18_clean.mp4")
    run(["ffmpeg", "-v", "error",
         "-ss", "8", "-t", "7", "-i", master,
         "-ss", "63", "-t", "7", "-i", master,
         "-ss", "133", "-t", "6", "-i", master,
         "-filter_complex",
         f"[0:v]crop=1080:1080:0:300,fps={FPS}[a];"
         f"[1:v]crop=1080:1080:0:600,fps={FPS}[b];"
         f"[2:v]crop=1080:1080:0:300,fps={FPS}[c];"
         f"[a][b][c]concat=n=3:v=1:a=0[o]",
         "-map", "[o]", "-an", "-c:v", "libx264", "-crf", "16",
         "-preset", "medium", c18, "-y"])
    b18 = _burn(tmp, "h18", c18,
                f"drawtext=fontfile={f_en}:text='cut-crossing mark':fontsize=52:"
                f"fontcolor=white@0.55:borderw=2:bordercolor=black@0.35:"
                f"x=(w-text_w)/2+90:y=200")
    out.append(("h18", "camera", "scene cut 2회 걸친 워터마크", 8, c18, b18))

    # h19: 대각 이동 오버레이(로고성 반투명 박스+텍스트)
    c19 = _clip(tmp, master, "h19", 112, 300)
    b19 = _burn(tmp, "h19", c19, None, complex_burn=lambda cl, bu: run(
        ["ffmpeg", "-v", "error", "-i", cl, "-filter_complex",
         f"color=c=0xFFFFFF@0.5:s=360x120:d={DUR}[card];"
         f"[0:v][card]overlay=x=80+t*38:y=120+t*30[base];"
         f"[base]drawtext=fontfile={f_en}:text='MOVING AD':fontsize=40:"
         f"fontcolor=0x101010:x=100+t*38:y=158+t*30[o]",
         "-map", "[o]", "-c:v", "libx264", "-crf", "16",
         "-preset", "medium", bu, "-y"]))
    out.append(("h19", "camera", "대각 이동 반투명 로고 박스", 112, c19, b19))

    # h20: 줌인(스케일 증가 팬) + 간헐 자막(2~6,11~16s)
    c20 = os.path.join(tmp, "h20_clean.mp4")
    still20 = os.path.join(tmp, "h20_still.png")
    run(["ffmpeg", "-v", "error", "-ss", "52", "-i", master, "-frames:v", "1",
         "-vf", "scale=2600:-2", still20, "-y"])
    run(["ffmpeg", "-v", "error", "-loop", "1", "-i", still20, "-t", str(DUR),
         "-vf", (f"crop=1080:1080:(iw-1080)/2+40*sin(t/2):"
                 f"min(ih-1080\\,60+t*18),fps={FPS}"),
         "-an", "-c:v", "libx264", "-crf", "16", "-preset", "medium", c20, "-y"])
    b20 = _burn(tmp, "h20", c20,
                f"drawtext=fontfile={f_kr}:text='흔들리는 카메라 간헐 자막':fontsize=50:"
                f"fontcolor=white:borderw=3:bordercolor=black@0.7:"
                f"x=(w-text_w)/2:y=h-300:enable='between(t,2,6)+between(t,11,16)'")
    out.append(("h20", "camera", "흔들림+수직 이동 간헐 자막", 52, c20, b20))

    return out


def ensure_projects():
    src = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                       params={"id": f"eq.{SRC_PROJECT}", "select": "user_id"},
                       headers=sbh(), timeout=30)
    src.raise_for_status()
    uid = src.json()[0]["user_id"]
    for i in range(1, 21):
        pid = f"beac0007-0000-4000-8000-0000000000{i:02d}"
        h = f"h{i:02d}"
        r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                         params={"id": f"eq.{pid}", "select": "id"},
                         headers=sbh(), timeout=30)
        r.raise_for_status()
        if r.json():
            continue
        ins = {"id": pid, "user_id": uid, "title": f"[v32-holdout] {h}",
               "source_path": f"holdout/{h}.mp4", "objective": "wm_remove",
               "status": "wm_queued", "status_detail": "v32 blind holdout",
               "wm_mode": "auto", "wm_tier": "fast"}
        r2 = requests.post(f"{SB_URL}/rest/v1/sc_projects",
                           headers=sbh({"Content-Type": "application/json",
                                        "Prefer": "return=minimal"}),
                           data=json.dumps(ins), timeout=30)
        r2.raise_for_status()
    print("[holdout] 프로젝트 행 20개 확인")


def main():
    tmp = tempfile.mkdtemp(prefix="holdout-")
    master = os.path.join(tmp, "master.mp4")
    print("[holdout] 마스터 다운로드")
    download("videos-clips", MASTER_PATH, master)
    items = build(tmp, master)
    rows = []
    for hid, typ, desc, ss, clean, inp in items:
        upload("videos-clips", f"{HOLD_PFX}/{hid}_clean.mp4", clean)
        upload("videos-clips", f"{HOLD_PFX}/{hid}_input.mp4", inp)
        upload("videos-source", f"holdout/{hid}.mp4", inp)
        rows.append({"id": hid, "type": typ, "desc": desc, "ss": ss,
                     "sha256_input": sha256(inp), "bytes_input": os.path.getsize(inp),
                     "sha256_clean": sha256(clean),
                     "bytes_clean": os.path.getsize(clean)})
        print(f"[holdout] {hid} {typ} 업로드 완료")
    ensure_projects()
    with open("HOLDOUT_MANIFEST.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("[holdout] manifest 20행 작성 완료")
    for r in rows:
        print(f"[HOLDOUT] {r['id']} {r['type']} {r['sha256_input']} "
              f"{r['bytes_input']}")


if __name__ == "__main__":
    main()
