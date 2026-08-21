# -*- coding: utf-8 -*-
"""SVOR 스테이징 실행기 (GitHub Actions에서 호출, 명세 G6).

SVOR_OP:
  download          가중치 Volume 적재 + SBOM sha256 전수 대조
  smoke             H100!/H200 각각 smoke (합성 81f, BF16, 20 steps)
  smoke_h100 / smoke_h200   한쪽만
  roi               SVOR_SPEC JSON(리스트)을 순서대로 실행
    [{"video":"bucket:path","mask":"bucket:path","out":"bucket:path",
      "gpu":"h100"|"h200","lora":"stage12"|"none","steps":20,...}, ...]
"""
import json
import os
import sys

import modal

APP = "inbilab-wm-svor-staging"


def main():
    op = os.environ.get("SVOR_OP", "smoke").strip()
    fail = 0
    if op == "download":
        fn = modal.Function.from_name(APP, "download_models")
        out = fn.remote({})
        print("[SVOR][download]", json.dumps(out)[:4000])
        if not out.get("ok"):
            fail += 1
    elif op.startswith("smoke"):
        targets = []
        if op in ("smoke", "smoke_h100"):
            targets.append(("svor_h100", "smoke_h100"))
        if op in ("smoke", "smoke_h200"):
            targets.append(("svor_h200", "smoke_h200"))
        for fname, tag in targets:
            fn = modal.Function.from_name(APP, fname)
            try:
                out = fn.remote({"op": "smoke", "tag": tag})
            except Exception as e:  # noqa: BLE001 — 실패도 결과로 기록
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            print(f"[SVOR][{tag}]", json.dumps(out)[:4000])
            if not out.get("ok"):
                fail += 1
    elif op == "roi":
        spec = json.loads(os.environ.get("SVOR_SPEC", "[]"))
        for i, item in enumerate(spec):
            fname = "svor_h200" if item.get("gpu") == "h200" else "svor_h100"
            fn = modal.Function.from_name(APP, fname)
            ev = {"op": "roi", **item}
            try:
                out = fn.remote(ev)
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            print(f"[SVOR][roi:{i}]", json.dumps(out)[:4000])
            if not out.get("ok"):
                fail += 1
    else:
        print(f"[SVOR] unknown SVOR_OP={op}")
        fail += 1
    print(f"[SVOR] done fail={fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
