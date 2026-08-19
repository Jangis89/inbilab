# V32 RC1 Release Manifest (Phase 1 — 2026-08-19)

전환 대상은 이 문서에 기록된 상태 **하나로 고정**한다. 전환 과정에서 알고리즘·
파라미터 실험을 하지 않는다 (버그는 별도 hotfix 브랜치 → 골든/validator/최소 벤치
→ 새 RC 태그 절차로만).

## 동결 지점

| 항목 | 값 |
|---|---|
| branch | `perf/wm-v32-remaining-production-readiness` |
| 파이프라인 코드 commit | **885a8a9a** (`gpu-worker/handler_v32.py` — 이후 커밋은 docs 8건 + 골든 게이트 스크립트 1건뿐, 파이프라인 무변경을 compare API로 확인) |
| tag | **`wm-v32-rc1-hardened`** @ de9c5257 (Pre-release 게시, 2026-08-19) |
| rollback baseline | `wm-v32-rc0-speed-baseline` @ 894fff7a — 강등 배포 49초 실측 |
| Modal app | `inbilab-wm-gpu-v32-speed-staging` (workspace jangis89 / env main) |
| deployment workflow | `.github/workflows/modal-deploy-v32-staging.yml` (main), action=deploy, ref 지정 |
| container image | pytorch 2.7.1-cuda12.8 + minimax-remover /models (L40S, cpu8/mem64G, scaledown 300s, max_containers 32) |
| handler | handler_v32.py (V32 파이프라인 전체 내장) |
| box detector | **v11** (그룹 시간축 집계 (α,C) 추정 un-blend + 불투명 AI(+scale1.0) + 지지도 오탐 필터 + 엣지 스냅 + rect 보간 + 개방 경계 16px 제한) |
| K | 12 |
| key_step | 5 |
| request_time_prewarm | 3 |
| permanent_min_containers | **0** |
| S3 업로드 | 멀티파트 16MB×8동시, region 자동 재시도, 실패 시 단일 PUT 폴백 |
| S3 자격 | Modal Secret `v32-staging-s3` (finish 함수에만 주입) — **production key 미발급** |
| validator | Layer1 항상(4.7초, 11/12 검출·오탐 0) + deep audit 플래그(12/12) |
| queue | Supabase `wm_v32_queue` (영속 FIFO, unique partial index 중복거부, heartbeat 20s/stale 120s, active_gpu_jobs=1, finish overlap) |

## 동결 시점 공식 성능·품질 (원자료: docs/FINAL_*, GOLDEN_*)

- warm10: P50 **182.3s** / P95 206.4s, 10/10
- cold5: 4/5 ≤300s, 1/5 = 669.7s (Modal GPU 배정 이상치 — 코드 무관)
- 혼잡10: 처리 P50 207s, 10/10, ETA 10/10 적중
- 합계 25/25 성공 (실패율 0%), 처리 P50 201s / P95 300.1s
- 골든 15/15 (박스 5종 포함; 전체폭 반투명 바 2종은 최악 프레임 옅은 잔존 한계 기록)
- 원가: warm 177초 기준 약 $0.68/작업 (공시요율×실측 + 대시보드 CPU/Mem 비율 보정)

## Phase 1.2 운영 상태 검증 (2026-08-19 실사, 전부 확인됨)

| 확인 항목 | 방법 | 결과 |
|---|---|---|
| v29 운영 정상 | Modal Apps: `inbilab-wm-gpu` Live (api/process) | ✅ |
| V32 일반 고객 routing 0% | GitHub main 트리 전수 검색 — V32 관련 파일은 staging workflow 1개뿐, gpu-worker에 v29 handler만 존재 (구조적으로 0%) | ✅ |
| production S3 key 없음 | Modal Secrets: `v32-staging-s3`, `inbilab-supabase` 2개만 존재 | ✅ |
| permanent GPU 0 | Modal Containers: "0 containers running" | ✅ |
| staging key만 존재 | 위 Secrets 목록과 동일 | ✅ |

## 다음 단계

Phase 2: 대표 UAT (staging 경로, 신규 영상 3종) → 통과 시 Phase 3 production Secret.
