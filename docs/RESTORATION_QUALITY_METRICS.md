# RC3 복원 품질 지표 정의 + RC2 기준선 (Phase C)

- 날짜: 2026-08-20 · 스크립트: `gpu-worker/scripts/evaluate_restoration_quality.py`
- 원칙(스펙 C.4/C.5): **자동지표 = 개선방향·회귀탐지용. 최종 gate는 대표의 정상속도(1×) 육안 판정.**

## 지표 정의

| 지표 | 의미 (쉬운 설명) | 계산 |
|---|---|---|
| sharp_p50 / sharp_p10 | 지운 자리의 질감이 주변과 같은 정도 (1.0=티 안 남, 낮을수록 뿌옇게 뭉갬) | 복원영역 vs 주변 링의 Laplacian 선명도비 |
| stain_frames_lt06 | 얼룩이 심한(선명도비<0.6) 프레임 수 | 1fps 전수 |
| cast_p90 | 지운 자리가 주변과 색이 다른 정도 | 복원영역-링 채널 최대 색차 p90 |
| **stain_composite** | 얼룩 종합점수 (낮을수록 좋음) | mean(1-sharp) + mean(cast)/100 |
| glyph_resid_p90 | 지운 영역 안에 남은 글자 획 픽셀 수 | 어두운 획 검출 |
| card_edge_p50/p90 | 카드 경계선이 보이는 정도 | 카드 테두리 밴드 gradient 에너지 |
| card_lift / absl | 카드 내부가 주변보다 밝거나 어두운 정도 | 내부-링 밝기차 |
| **card_residual_score** | 카드 잔존 종합 (낮을수록 좋음) | edge_p50 + |lift|_p50 |
| flicker_ratio | 지운 자리가 프레임마다 흔들리는 정도 (1.0=원본 수준) | optical-flow 정렬 warp error, 출력/원본 비 |
| roi_psnr / roi_ssim / hf_ratio | (GT 있는 골든만) 복원영역 정확도·질감 보존율 | clean control re-encode 대비 |

합성 골든은 clean original이 아니라 **clean control re-encode** 대비로 평가한다(재인코딩 손실을 복원 결함으로 오인 금지).

## RC2 기준선 (실측, 1fps 전수 — RESTORATION_QUALITY_BASELINE_RC2.csv)

| 지표 | UAT-01 | UAT-02 | UAT-03 |
|---|---|---|---|
| sharp_p50 | 0.700 | 0.935 | 0.976 |
| sharp_p10 | 0.403 | 0.615 | 0.674 |
| stain_frames_lt06 | **58/175** | 17/175 | 6/130 |
| cast_p90 | 45.3 | 54.0 | 19.9 |
| stain_composite | **0.531** | 0.406 | 0.232 |
| glyph_resid_p90 | 2439 | 2960 | 2337 |
| card_residual_score | – | **80.1** (edge 48.5 + lift 17.2) | – |
| flicker_ratio | 1.30 | **1.89** | 1.04 |

## RC3 통과 목표 (결과 나온 뒤 낮추지 않는다 — 스펙 C.6)

| 목표 | 기준 |
|---|---|
| stain_composite | RC2 대비 **30%+ 개선**: UAT-01 ≤0.372 · UAT-02 ≤0.284 · UAT-03 ≤0.162 |
| card_residual_score | RC2 대비 **70%+ 개선**: UAT-02 ≤ **24.0** (참고: 원본 자체의 경계 에너지 수준 ≈25) |
| flicker_ratio | 전 UAT ≤ 1.25 |
| glyph_resid_p90 | 전 UAT ≤ 300 (readable text residual 0 목표) |
| 기존 detection | 골든 25/25 유지, 실물 오탐 0 (recall 악화 0) |
| 대표 육안 | 1× 재생에서 지운 자리·얼룩·카드·그래픽 끊김이 즉시 눈에 띄지 않음 (최종 gate) |

## 참고 — Phase B 프로토타입 사전 검증 (로컬 CPU, AI 미적용)

카드 전 구간 un-blend(rounded matte, α=0.5/C=254) + 글자·링 임시 채움만으로:
card edge p50 48.5→18.8, |lift| 급감. AI 정밀 복원(GPU)이 글자·링을 맡으면 목표(≤24) 달성 가능성 높음.
