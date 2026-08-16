# CURRENT_HEAD_DIFF_FROM_AUDIT.md

- 작성: 2026-08-16 (V31 단계 0)
- 현재 HEAD: `c5f8ee0f908a43a53d23bd914b2dcfb706c40ec3`
- 감사 커밋: `c5f8ee0f908a43a53d23bd914b2dcfb706c40ec3`

## 결론
**현재 HEAD == 감사 커밋 (diff 0건).** 감사 패키지는 오늘(2026-08-16) 이 커밋에서 생성됐고, 이후 저장소 변경이 없다.

- 두 커밋 사이 변경 파일: 없음
- 성능 파이프라인 관련 변경: 없음 (감사 자체가 최신 상태 반영)
- 이미 해결된 감사 항목: v30.1 지각 여유 2.5배(감사 직전 커밋에 포함), 시간표 초기화(운영 app_settings에서 wm_stage_stats 삭제됨 — DB 상태이며 코드 아님)
- 새로 생긴 위험: 없음
- 적용 방침: 명세서를 감사 커밋 기준 그대로 적용. 원본 작업공간 미커밋 변경 없음(fresh clone) — stash/삭제 불필요

## 현재 배포·설정 스냅샷 (2026-08-16 심야 기준, 실측)
- Modal 운영 앱: inbilab-wm-gpu (api CPU / process L40S), 커밋 c5f8ee0 기준 배포
- Railway 감시원: v30.1 ACTIVE ×3 replicas
- RunPod 예비: las84wgugy6v6k (Max 19 + H100 1)
- app_settings: wm_backend='modal', wm_stage_stats=삭제됨(재학습 중, 이후 Modal 표본 축적), wm_slow_mult_x10 미설정(기본 25=2.5배), wm_workers_per_job 기본 5, wm_congest_jobs=3, wm_stall_sec=60, wm_stall_first_sec=120, wm_orphan_min=10
