# PERF_BASELINE_V31.md — 단계 0 현상 고정

- 기준 커밋: c5f8ee0 (감사 커밋 == 현재 HEAD)
- 기준 영상: benchmark_input_full.mp4 (=sc_projects 31118dec-b65d-4d99-b67e-61ab3333094b 원본)
  - 177.33초, 1080×1920, 30fps, 5,320프레임, 102.6MB, auto 모드, fast 등급, 감지 4영역

## v29 baseline 실행 (현재 HEAD 배포본, 2026-08-16 15:41 실측)
명세의 "동일 영상 v29 1회 재실행"은 감사 당일 이미 수행됨 — 현재 HEAD가 배포된 운영 Modal에서
15:41:25→16:01:00 실행이 곧 현행 코드의 baseline이다 (감사 커밋과 HEAD가 동일하므로 재실행과 등가).

```text
total wall: 1,175초 (worker sec=1171)  RTF 6.63
plan  478.4s: dl_cnt 34.2 / scan_dec 11.4 / scan 42.2 / mask_dec 80.2 / masks 310.2 / plan_up 0.2
workpool(×5) wall≈426s: dec 290.9~320.1 / ai 60.8~123.7 / model 4.6~5.4 / enc_up 23.9~32.2
mergeseg(×5) wall≈187s: chunks(dl) 62.6~98.1 / comp 39.5~86.6
finish 22.4s
결과 357.5MB, 비용 ≈ $1.9, 감시 개입 0회
```

참고 보조 표본(동일 영상, 같은 날):
- RunPod 단독: 실패 (감지 단계, 30분) — backend 편차 표본
- Modal 1차+failover: 53분 (지각 오탐 사건, v30.1로 수정 완료)

## V31 판정 기준 재확인
- 1차 통과 P50 ≤480 / P95 ≤600
- 상품 P50 ≤240 / P95 ≤360
- 경쟁 P50 ≤177 / 최종 P50 ≤150
- 우선순위: 품질 > P95 > P50 > 실패율 > 크기 > 비용

## 벤치마크 프로젝트 격리 방침
스테이징 실행은 31118dec 원본 파일을 참조하되, **별도 복제 프로젝트 행**(전용 UUID)과
`wmtmp-v31/{pid}` prefix, `pipeline_ver='v31'`로 운영 데이터와 완전 분리한다.
