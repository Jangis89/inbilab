# RC4 blind holdout 동결 선언 (Phase B 완료)

- 동결 시각: 2026-08-20 (workflow run #145, `holdout` 액션)
- 생성기: `gpu-worker/scripts/make_holdout_v32.py` (RC4 브랜치)
- 구성: 사람 5(h01~05) · 카드 5(h06~10) · 반복패턴 5(h11~15) · 카메라이동/컷 5(h16~20)
- 소재: `bench-assets/benchmark_master.mp4` (권리 허용 마스터)에서 파생.
  골든(g01~g35)과 다른 구간·다른 스타일만 사용. 전 항목 clean GT 쌍 보유.
- 저장:
  - 입력/GT: `videos-clips/bench-assets/holdout/hNN_{input,clean}.mp4` (정리 비대상)
  - 실행용: `videos-source/holdout/hNN.mp4`, 행 `beac0007-…NN`
  - 전체 manifest(GT 해시 포함) 원본: Actions artifact `holdout-v32-32418545120`
- 입력 20종 SHA256: `RC4_BLIND_HOLDOUT_MANIFEST.csv` (이 커밋)

## 동결 규칙 (명세 REV2 §B)

1. 개발 기간 중 이 20종에 파이프라인을 돌려 지표를 보고 threshold를 조정하는
   행위 금지. 개발 신호는 골든 35종과 로컬 합성 시험만 사용한다.
2. holdout 실행은 Phase L 채점 시점에 수행한다 (실행 자체를 늦춰 오염 차단).
3. 등급: A(원본처럼 자연) / B(1× 재생 시 무리 없음) / C(정지화면에서 흠) /
   D(재생 중 눈에 띄는 결함). 합격: A+B ≥ 18, C ≤ 2, D = 0.

## 정직 고지 (다양성 한계)

20종 모두 단일 마스터 영상에서 파생했다(권리가 확인된 유일한 소재).
배경 다양성은 구간·crop·변형으로 확보했으나 완전히 독립적인 영상 20개보다는
약하다. 대표가 권리 허용 영상을 추가로 제공하면 h21+로 확장한다(기존 20종의
동결은 유지 — 추가만 허용).
