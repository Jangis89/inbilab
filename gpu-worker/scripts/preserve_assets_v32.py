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


def copy_obj(src, dst):
    r = requests.post(f"{SB_URL}/storage/v1/object/copy",
                      headers=hdr({"Content-Type": "application/json"}),
                      data=json.dumps({"bucketId": BUCKET, "sourceKey": src,
                                       "destinationKey": dst}), timeout=120)
    if r.status_code == 400 and "already exists" in r.text.lower():
        return "exists"
    if not r.ok:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    return "copied"


def hash_obj(path):
    h = hashlib.sha256()
    n = 0
    with requests.get(f"{SB_URL}/storage/v1/object/{BUCKET}/{path}",
                      headers=hdr(), stream=True, timeout=600) as r:
        if not r.ok:
            return None, f"HTTP {r.status_code}"
        for chunk in r.iter_content(1 << 20):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def main():
    spec = os.environ.get("PRESERVE_SPEC", "").strip()
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
