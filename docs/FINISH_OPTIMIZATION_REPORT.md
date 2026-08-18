# V32 Phase 1 — finish 스트리밍 최적화 A/B 결과 (2026-08-18)

## 측정 조건
- 구성: K=12, key_step=5, warm=12, crf18, 한산 시간대, 반복 3회
- 대상: 기준 영상 177.33s / 1080x1920 / 30fps / 5,320프레임
- BEFORE = run 32032011901 (p50-v3, non-stream finish)
- AFTER  = run 32096394142 (p1-stream, streaming finish: 세그먼트 완료 즉시 병렬 다운로드+프레임 검증)

## 결과 (세그먼트 완료 후 finish 구간 = tail)
| 지표 | BEFORE | AFTER | 변화 |
|---|---|---|---|
| tail P50 | 130.8s | 108.5s | -22.3s |
| tail 범위 | 115.6~133.7 | 98.7~113.7 | |
| 전체 total P50 | 355.5s | 335.9s | -19.6s |
| dl+verify (tail 내) | 약 19~43s | 0s (세그 실행과 겹침) | 제거됨 |
| concat | 9.2~9.4s | 16.8~17.9s | +7~8s (다운로드 직후 캐시 미스 추정) |
| upload | 78.7~87.7s | 74.8~85.9s | 변화 없음 |

## 판정
- 스트리밍 finish 효과 확인됨: 다운로드+검증이 GPU 세그먼트 실행과 완전히 겹쳐져 tail에서 사라짐.
- 남은 tail(약 100~110s)의 구성: upload 75~86s (약 80%) + concat 17s.
- **finish 목표 P50 ≤45s는 upload 가속 없이는 불가능** — 단일 PUT 업로드(약 100MB)가 병목.
- 다음 단계: Supabase S3 프로토콜 multipart 병렬 업로드 (S3 access key 필요, 사장 액션).
  예상: 85s → 20~25s, tail 108 → 약 45~50s, total P50 335 → 약 275s.
- 추가 무비용 후보(다음 커밋): scan 단계 원본 다운로드(dl_cnt 78s)를 HTTP Range 병렬 다운로드로 단축.

## 참고 관측
- warm=12에도 alloc_wait 최대 50~112s 발생(런0은 중앙값 56s) — 배정 지각은 여전히 존재. Phase 8 공용큐+hedge에서 해결 예정.
