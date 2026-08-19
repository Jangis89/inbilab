# V32 Rollback Dry-run 보고 (Phase J — 2026-08-19, staging 실측)

## 강등·복구 실측 (production-like: 같은 workflow 경로)

| 단계 | 실측 | 게이트 | 판정 |
|---|---|---|---|
| RC0 태그로 강등 배포 (`wm-v32-rc0-speed-baseline`) | **49초** (05:41:12→05:42:01, run 32220353622) | 5분 | 통과 |
| 최신(v11)으로 복원 배포 | **46초** (05:45:21→05:46:07, run 32220621075) | 5분 | 통과 |

절차 = GitHub Actions deploy 액션에 태그/브랜치만 바꿔 실행 → 별도 스크립트 불필요.
운영 강등 시나리오: 운영 감시원이 v32 이상 감지 → 동일 deploy 액션으로 직전 안정
태그 재배포 → 1분 내 완료. (운영은 현재 v29가 처리 중이므로 실제 강등 대상은
canary 전환분에만 해당.)

## 장애 5종 대응 실증 매핑

| 장애 | 대응 설계 | 실증 증거 | 상태 |
|---|---|---|---|
| ① S3 자격/리전 오류 | region 후보 자동 재시도 + 멀티파트 실패 시 단일 PUT 폴백 | Phase 1에서 실제 403→재시도로 해결 (FINISH_OPTIMIZATION_REPORT) | **실증됨** |
| ② GPU 스트래글러 | hedge 120s(first-valid-wins) + 1500s 타임아웃 + 세그 실패 카운트→FAIL | A3 실험(hedge 11발사 실측), ReadTimeout 방어 수정 | **실증됨** |
| ③ queue 러너 사망 | heartbeat 20s + stale 120s 복구 + attempt_token 중복 방지 | Phase E: 러너 kill 90s → 러너 B 복구 → 3/3 완료 (QUEUE_INTEGRATION_V32) | **실증됨** |
| ④ 손상 출력 | Layer1 검증(모든 finish) → 실패 시 FAIL, 서명 URL 미발급 | fault-injection F01~F12: L1 11/12 + deep audit 12/12 | **실증됨(로컬)** — staging 실경로 주입은 미실시 |
| ⑤ GPU 배정 지연(콜드 폭풍) | request-time prewarm 3 + ETA 범위 안내 | cold-4에서 실제 발생(669.7s), ETA 로직은 burst10에서 10/10 적중 | **관측됨** — 근본 해결 불가(인프라), 노출 관리로 대응 |

미실시 항목 정직 고지: ④의 "staging 실경로에 손상 세그를 실제 주입"은 저장소
조작이 필요해 생략 — 로컬 완전 재현(같은 코드 경로)으로 대체. C0 기간에
deep_audit 100%로 상시 감시하므로 리스크 낮음.
