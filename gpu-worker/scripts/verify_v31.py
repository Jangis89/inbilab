# -*- coding: utf-8 -*-
"""V31 결과물 품질 검증 (단계 3/11.5 게이트 일부).

벤치 프로젝트의 V31 결과(wm_v31_{BENCH_PID}.mp4)를 내려받아
- 프레임 수 == 기준 N (누락·중복 0)
- 길이·오디오 스트림 존재
- v29 기준 결과(31118dec 프로젝트의 wm_done 결과)와 PSNR/SSIM 비교
를 수행한다. GitHub Actions 러너에서 실행 (SUPABASE_URL/SERVICE_ROLE 필요).

주의: v31은 중간 재인코딩 1회가 없으므로 v29 결과와 byte 동일이 아니라
'시각적 동일(고 PSNR/SSIM)'이 기대치다. AI 산출 자체의 동일성은
test_equivalence_v31.py(byte equality)가 담당한다.
"""
import json, os, re, subprocess, sys, tempfile

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
BENCH_PID = "beac0001-0000-4000-8000-000000000031"
REF_PID = "31118dec-b65d-4d99-b67e-61ab3333094b"
EXPECT_N = 5320


def sbh():
    return {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}


def get_row(pid, sel):
    r = requests.get(f"{SB_URL}/rest/v1/sc_projects",
                     params={"id": f"eq.{pid}", "select": sel}, headers=sbh(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(f"프로젝트 행 없음: {pid}")
    return rows[0]


def storage_get(bucket, path, dest):
    r = requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                     headers=sbh(), stream=True, timeout=900)
    r.raise_for_status()
    n = 0
    with open(dest, "wb") as f:
        for ch in r.iter_content(1 << 20):
            f.write(ch); n += len(ch)
    return n


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries",
         "stream=nb_read_frames,avg_frame_rate,width,height:format=duration",
         "-of", "json", path], capture_output=True, text=True).stdout
    j = json.loads(out)
    a = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name", "-of", "json", path], capture_output=True, text=True).stdout
    ja = json.loads(a)
    st = j["streams"][0]
    return {"frames": int(st.get("nb_read_frames") or 0),
            "w": st.get("width"), "h": st.get("height"),
            "fps": st.get("avg_frame_rate"),
            "dur": round(float(j.get("format", {}).get("duration") or 0), 2),
            "audio": [s.get("codec_name") for s in ja.get("streams", [])]}


def metric(a, b, flt):
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", a, "-i", b,
                        "-lavfi", f"[0:v][1:v]{flt}", "-f", "null", "-"],
                       capture_output=True, text=True)
    return p.stderr


def main():
    tmp = tempfile.mkdtemp()
    row = get_row(BENCH_PID, "user_id,status,status_detail")
    uid = row["user_id"]
    v31_path = f"{uid}/wm_v31_{BENCH_PID}.mp4"
    v31 = os.path.join(tmp, "v31.mp4")
    n1 = storage_get("videos-clips", v31_path, v31)
    print(f"[verify] v31 결과 다운로드 {n1/1e6:.1f}MB — {v31_path}")

    ref_row = get_row(REF_PID, "status,status_detail")
    det = ref_row.get("status_detail") or {}
    if isinstance(det, str):
        det = json.loads(det)
    url = det.get("url") or ""
    m = re.search(r"/videos-clips/([^?]+)", url)
    if not m:
        raise RuntimeError(f"v29 기준 결과 경로를 status_detail.url에서 못 찾음: {url[:120]}")
    ref_path = m.group(1)
    ref = os.path.join(tmp, "v29.mp4")
    n2 = storage_get("videos-clips", ref_path, ref)
    print(f"[verify] v29 기준 다운로드 {n2/1e6:.1f}MB — {ref_path}")

    p1, p2 = probe(v31), probe(ref)
    print("[verify] v31 probe:", json.dumps(p1, ensure_ascii=False))
    print("[verify] v29 probe:", json.dumps(p2, ensure_ascii=False))

    hard_fail = []
    if p1["frames"] != EXPECT_N:
        hard_fail.append(f"v31 프레임 수 {p1['frames']} != {EXPECT_N}")
    if not p1["audio"]:
        hard_fail.append("v31 오디오 스트림 없음")
    if (p1["w"], p1["h"]) != (p2["w"], p2["h"]):
        hard_fail.append(f"해상도 불일치 v31 {p1['w']}x{p1['h']} vs v29 {p2['w']}x{p2['h']}")

    psnr_avg = ssim_all = None
    if p1["frames"] == p2["frames"]:
        e = metric(v31, ref, "psnr")
        mm = re.search(r"average:([\d.]+|inf)", e)
        psnr_avg = mm.group(1) if mm else None
        e = metric(v31, ref, "ssim")
        mm = re.search(r"All:([\d.]+)", e)
        ssim_all = mm.group(1) if mm else None
    else:
        print(f"[verify] 프레임 수 다름(v31 {p1['frames']} vs v29 {p2['frames']}) — PSNR 생략")

    summary = {"v31_frames": p1["frames"], "v29_frames": p2["frames"],
               "v31_dur": p1["dur"], "v29_dur": p2["dur"],
               "v31_audio": p1["audio"], "psnr_avg": psnr_avg, "ssim_all": ssim_all,
               "v31_bytes": n1, "v29_bytes": n2, "hard_fail": hard_fail}
    print("\n[VERIFY]", json.dumps(summary, ensure_ascii=False))
    if hard_fail:
        sys.exit(1)
    print("[verify] 하드 게이트 통과 ✅ (프레임 수·오디오·해상도)")


if __name__ == "__main__":
    main()
