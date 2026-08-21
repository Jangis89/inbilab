# -*- coding: utf-8 -*-
"""V32 기준물 보존 복사기 (RC4 Phase A/B 지원).

운영 24h 원본정리 정책이 건드리지 않는 bench-assets/ 아래로
스토리지 객체를 서버측 복사해 보존한다 (service role 필요, staging 전용).

- 기본: UAT RC2/RC3 출력 6종을 bench-assets/uat-preserve/ 로 복사
- PRESERVE_SPEC 환경변수로 임의 쌍 지정 가능: "src>dst,src>dst" (bucket 내 경로)
- 복사 후 각 대상 객체를 내려받아 SHA256·바이트를 출력 (검증용)

운영 v29 무접촉: 읽기(원본)와 쓰기(bench-assets/)만 수행, 행/정책 변경 없음.
"""
import hashlib
import json
import os
import sys

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]
BUCKET = "videos-clips"
UID = "4117e902-3396-4b14-aea0-957b326ab563"

DEFAULT_PAIRS = [
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000001.mp4",
     "bench-assets/uat-preserve/rc3_o1.mp4"),
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000002.mp4",
     "bench-assets/uat-preserve/rc3_o2.mp4"),
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000003.mp4",
     "bench-assets/uat-preserve/rc3_o3.mp4"),
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000044.mp4",
     "bench-assets/uat-preserve/rc2_o1.mp4"),
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000047.mp4",
     "bench-assets/uat-preserve/rc2_o2.mp4"),
    (f"{UID}/wm_v32_beac0005-0000-4000-8000-000000000046.mp4",
     "bench-assets/uat-preserve/rc2_o3.mp4"),
]


def hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def split_bucket(p):
    """'bucket:path' → (bucket, path); 접두사 없으면 기본 BUCKET."""
    if ":" in p.split("/", 1)[0]:
        b, rest = p.split(":", 1)
        return b, rest
    return BUCKET, p


def copy_obj(src, dst):
    sb, sp = split_bucket(src)
    db, dp = split_bucket(dst)
    body = {"bucketId": sb, "sourceKey": sp, "destinationKey": dp}
    if db != sb:
        body["destinationBucket"] = db
    r = requests.post(f"{SB_URL}/storage/v1/object/copy",
                      headers=hdr({"Content-Type": "application/json"}),
                      data=json.dumps(body), timeout=120)
    if r.status_code == 400 and "already exists" in r.text.lower():
        return "exists"
    if not r.ok:
        # 교차 버킷 미지원 등 — 다운로드→업로드 폴백 (service role)
        with requests.get(f"{SB_URL}/storage/v1/object/{sb}/{sp}",
                          headers=hdr(), stream=True, timeout=1800) as g:
            if not g.ok:
                return f"HTTP {r.status_code}/{g.status_code}: {r.text[:120]}"
            data = g.content
        u = requests.post(f"{SB_URL}/storage/v1/object/{db}/{dp}",
                          headers=hdr({"Content-Type": "video/mp4",
                                       "x-upsert": "true"}),
                          data=data, timeout=1800)
        if not u.ok:
            return f"fallback HTTP {u.status_code}: {u.text[:120]}"
        return "copied-fallback"
    return "copied"


def hash_obj(path):
    b, p = split_bucket(path)
    h = hashlib.sha256()
    n = 0
    with requests.get(f"{SB_URL}/storage/v1/object/{b}/{p}",
                      headers=hdr(), stream=True, timeout=600) as r:
        if not r.ok:
            return None, f"HTTP {r.status_code}"
        for chunk in r.iter_content(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _dl(bucket, path, fp):
    with requests.get(f"{SB_URL}/storage/v1/object/{bucket}/{path}",
                      headers=hdr(), stream=True, timeout=1800) as r:
        if not r.ok:
            return False
        with open(fp, "wb") as f:
            for ch in r.iter_content(1 << 20):
                f.write(ch)
    return True


def contact_mode(spec):
    """RC4 리뷰용 contact sheet: IN|OUT|GT|DIFF(×4 증폭) 가로 4패널 PNG를
    videos-clips/{UID}/rc4_review/ 에 업로드 (사용자 세션 서명 가능 경로).

    spec 예: contact:g27@1,5,10|g28@2,7
    """
    import subprocess
    import tempfile
    tmp = tempfile.mkdtemp(prefix="contact-")
    jobs = []
    for part in spec.split("|"):
        g, ts = part.split("@", 1)
        jobs.append((g.strip(), [float(x) for x in ts.split(",") if x.strip()]))
    fail = 0
    for g, times in jobs:
        gnum = int(g[1:])
        pid = f"beac0002-0000-4000-8000-0000000000{gnum:02d}"
        inp = os.path.join(tmp, f"{g}_in.mp4")
        out = os.path.join(tmp, f"{g}_out.mp4")
        gt = os.path.join(tmp, f"{g}_gt.mp4")
        ok_in = _dl(BUCKET, f"bench-assets/golden/{g}_input.mp4", inp)
        ok_out = _dl(BUCKET, f"{UID}/wm_v32_{pid}.mp4", out)
        ok_gt = _dl(BUCKET, f"bench-assets/golden/{g}_clean.mp4", gt)
        print(f"[CONTACT] {g} dl in={ok_in} out={ok_out} gt={ok_gt}")
        if not (ok_in and ok_out):
            fail += 1
            continue
        for t in times:
            png = os.path.join(tmp, f"{g}_t{t:g}.png")
            if ok_gt:
                fc = ("[0:v]scale=360:-2[a];[1:v]scale=360:-2[b];"
                      "[2:v]scale=360:-2[c];[3:v]scale=360:-2[b2];"
                      "[4:v]scale=360:-2[c2];"
                      "[b2][c2]blend=all_mode=difference,lutyuv=y=val*4[d];"
                      "[a][b][c][d]hstack=4")
                cmd = ["ffmpeg", "-v", "error",
                       "-ss", str(t), "-i", inp, "-ss", str(t), "-i", out,
                       "-ss", str(t), "-i", gt, "-ss", str(t), "-i", out,
                       "-ss", str(t), "-i", gt,
                       "-frames:v", "1", "-filter_complex", fc, png, "-y"]
            else:
                fc = ("[0:v]scale=360:-2[a];[1:v]scale=360:-2[b];"
                      "[a][b]hstack=2")
                cmd = ["ffmpeg", "-v", "error",
                       "-ss", str(t), "-i", inp, "-ss", str(t), "-i", out,
                       "-frames:v", "1", "-filter_complex", fc, png, "-y"]
            r = subprocess.run(cmd, capture_output=True)
            if r.returncode != 0 or not os.path.exists(png):
                print(f"[CONTACT] {g} t={t} ffmpeg 실패: "
                      f"{r.stderr.decode()[-160:]}")
                fail += 1
                continue
            with open(png, "rb") as f:
                data = f.read()
            dst = f"{UID}/rc4_review/{g}_t{t:g}.png"
            u = requests.post(f"{SB_URL}/storage/v1/object/{BUCKET}/{dst}",
                              headers=hdr({"Content-Type": "image/png",
                                           "x-upsert": "true"}),
                              data=data, timeout=300)
            print(f"[CONTACT] {dst} bytes={len(data)} up={u.status_code}")
            if not u.ok:
                fail += 1
    print(f"[CONTACT] done fail={fail}")
    if fail:
        sys.exit(1)


def main():
    spec = os.environ.get("PRESERVE_SPEC", "").strip()
    if spec.startswith("contact:"):
        contact_mode(spec[len("contact:"):])
        return
    if spec:
        pairs = []
        for item in spec.split(","):
            s, d = item.split(">", 1)
            pairs.append((s.strip(), d.strip()))
    else:
        pairs = DEFAULT_PAIRS
    fail = 0
    for src, dst in pairs:
        st = copy_obj(src, dst)
        digest, size = hash_obj(dst)
        ok = digest is not None
        if not ok:
            fail += 1
        print(f"[PRESERVE] {dst} copy={st} sha256={digest} bytes={size}")
    print(f"[PRESERVE] done fail={fail}/{len(pairs)}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
