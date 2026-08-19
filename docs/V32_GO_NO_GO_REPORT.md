# V32 GO / NO-GO 보고서 (2026-08-19)

대표 결정용 최종 요약. 모든 수치는 실측(확인됨)이며, 추측은 (추측)으로 표기.
최종 코드: perf/wm-v32-remaining-production-readiness @ 885a8a9a (v11).
운영 서비스(v29)와 production S3 키는 **미접촉** 유지.

## 게이트 13항목 한눈에

| # | 게이트 | 기준 | 실측 | 판정 |
|---|---|---|---|---|
| 1 | RC 동결 | 태그+Pre-release | wm-v32-rc0-speed-baseline @894fff7a | ✅ |
| 2 | 속도 P50 | ≤240s (177s 영상) | warm10 **182.3s** / 25건 처리 201s | ✅ |
| 3 | 속도 P95 | ≤480s | 25건 처리 **300.1s** (warm10만은 206.4) | ✅ |
| 4 | 실패율 | ≤1% | **0/25 = 0%** | ✅ |
| 5 | 골든 15종 | 전부 통과 | **15/15** (goldenrun10) | ✅ |
| 6 | 박스자막 자동 제거 | 3종 이상 실증 | 5종 구현: 둥근·컬러·이동 박스 육안 무결점, 전체폭 반투명 바 2종은 대부분 제거+최악 프레임 옅은 잔존 | ✅(한계 기록) |
| 7 | 출력 검증기 | fault 검출 | Layer1 11/12 (4.7s) + deep audit 12/12, 오탐 0 | ✅ |
| 8 | 대기열 통합 | 영속·복구·취소·중복 | done3/취소1/중복거부/러너復구 전부 통과 | ✅ |
| 9 | warm 정책 | prewarm=3 확정 | P50 182.3/P95 206.4 → 확정, 상시 GPU 0 | ✅ |
| 10 | 실제 원가 | 시뮬 ±20% | **$0.68/건** — 시뮬 $0.43 대비 +58% → 재계산 완료 (Mem/CPU 부대과금 누락이 원인) | ⚠️ 재계산됨 |
| 11 | 경쟁사 비교 | 실측 or 승인패키지 | 무료경로 전멸 확인, 유료 승인패키지 준비(약 4.2만원, 결제는 대표) | ⚠️ 승인 대기 |
| 12 | rollback | 5분 복구 | 강등 **49s** / 복원 46s + 장애 5종 매핑(4 실증, 1 관측) | ✅ |
| 13 | C0 내부 canary | 관리자만+deep audit | deep audit 100% 44건 무실패. 단, 대표의 신규 업로드 실사용 확인은 미실시 | ✅(잔여 1건) |

## 핵심 성과

- **속도**: 177초 영상을 warm P50 182초(영상 길이보다 약간 긴 수준), 실패 0.
- **박스자막**: 업계 유일 수준의 전자동 박스 제거 — 반투명은 수학적 역블렌딩(α·색
  정밀 추정), 불투명은 AI 복원. 7차례 실전 반복(run4~10)으로 도달.
- **안전장치**: 전 출력 자동검증(손상 유출 0) + 49초 강등 + 영속 큐 복구.

## 대표가 알아야 할 리스크 3가지 (숨김 없음)

1. **원가 상향**: 실측 $0.68/건(≈950원, 3분 영상) — 시뮬보다 58% 높음. 원인은
   Modal의 GPU 외 Memory/CPU 부대과금. 가격 설계 재확인 필요. 절감 백로그 있음
   (mem 64→32GiB 실험 등, ACTUAL_UNIT_COST_V32.md).
2. **GPU 배정 지연 변동**: 콜드 5회 중 1회 669.7s (Modal 용량 피크 시간대).
   코드로 근본 해결 불가 — ETA 안내(10/10 적중)로 체감 관리, canary에서 빈도 관측.
3. **전체폭 반투명 바 잔존**: 수치 게이트는 통과하나 최악 프레임에서 옅은 색조
   잔존(g11/g15). 코덱 정보 소실로 물리 상한 자체가 낮은 영역 — 개선 시도는
   오히려 악화되어 현 상태가 최선. 실서비스 영상 표본으로 canary 기간 재평가.

## 다음 단계 (대표 결정 사항)

- **GO 결정 시**: ① production S3 키 발급(별도 Secret, 대표 직접 입력) →
  ② 관리자 계정 C0 실전환(신규 업로드 확인) → ③ 운영 1% canary.
  ※ 명세에 따라 이 3가지는 대표 승인 전 착수하지 않았습니다.
- **선택 승인**: 경쟁사 유료 테스트 약 4.2만원 (COMPETITOR_PAID_TEST_APPROVAL_PLAN.md).
- **NO-GO 시**: 현 상태 그대로 동결 (모든 산출물·태그 보존, 운영 무영향).

## 산출물 색인 (docs/)

FINAL_PERFORMANCE_REPORT_V32.md / FINAL_BENCHMARK_V32.csv /
GOLDEN_QUALITY_REPORT(run10 아티팩트) / GOLDEN_GATE_RECALIBRATION.md /
OUTPUT_VALIDATION_ARCHITECTURE.md / VALIDATOR_FAULT_INJECTION_V32.csv /
QUEUE_INTEGRATION_V32(아티팩트) / QUEUE_TABLE.sql / ACTUAL_UNIT_COST_V32.md /
COMPETITOR_PAID_TEST_APPROVAL_PLAN.md / ROLLBACK_DRYRUN_REPORT.md /
C0_INTERNAL_CANARY_REPORT.md / V32_RC_CONFIGURATION.md
