# -*- coding: utf-8 -*-
"""스토리지 목록·복원 유틸 (읽기 + bench-assets→videos-source 복사만).

배경: 운영에는 원본 24시간 정리 정책이 있어 videos-source의 업로드 원본과
골든이 시간이 지나면 사라진다(2026-08-22 실측: scan_v32가 400으로 실패).
분석 산출물은 bench-assets/에 있어 안전하지만, **새로 생성**하려면 원본이
그 자리에 다시 있어야 한다.

LS_SPEC (JSON):
  {"list": ["bench-assets/uat-src", "videos-source/golden", ...],
   "copy": [["bench-assets/uat-src/uat01.mp4",
             "videos-source/4117e902-.../1787283147962_n39il9u3f1.mp4"], ...]}
- list: 접두사별 객체 이름·크기 출력
- copy: Supabase storage copy API로 복사(원본 불변, 대상만 생성)
운영 v29 무접촉: DB 행·정책은 건드리지 않는다.
"""
import json
import os
import sys

import requests

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE"]


def hdr(extra=None):
    h = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
    if extra:
        h.update(extra)
    return h


def split_bucket(p):
    parts = p.split("/", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def ls(prefix):
    b, p = split_bucket(prefix)
    r = requests.post(f"{SB_URL}/storage/v1/object/list/{b}",
                      headers=hdr({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": p, "limit": 1000,
                                       "sortBy": {"column": "name",
                                                  "order": "asc"}}),
                      timeout=120)
    r.raise_for_status()
    return r.json()


def copy_obj(src, dst):
    sb, sp = split_bucket(src)
    db, dp = split_bucket(dst)
    r = requests.post(f"{SB_URL}/storage/v1/object/copy",
                      headers=hdr({"Content-Type": "application/json"}),
                      data=json.dumps({"bucketId": sb, "sourceKey": sp,
                                       "destinationBucket": db,
                                       "destinationKey": dp}),
                      timeout=600)
    return r.status_code, r.text[:200]


def main():
    spec = json.loads(os.environ.get("LS_SPEC", "{}"))
    fail = 0
    for prefix in spec.get("list", []):
        try:
            items = ls(prefix)
            print(f"[LS] {prefix} -> {len(items)}개", flush=True)
            for it in items:
                meta = it.get("metadata") or {}
                sz = meta.get("size", "-")
                print(f"[LS]   {it.get('name')}  {sz}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[LS] {prefix} 실패: {type(e).__name__}: {e}")
            fail += 1
    for pair in spec.get("copy", []):
        src, dst = pair[0], pair[1]
        code, body = copy_obj(src, dst)
        ok = 200 <= code < 300
        print(f"[CP] {src} -> {dst} : {code} {'OK' if ok else body}",
              flush=True)
        if not ok:
            fail += 1
    print(f"[LS] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
