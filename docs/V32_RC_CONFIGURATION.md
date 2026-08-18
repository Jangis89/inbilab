# V32 Release Candidate 0 — 동결 구성 (2026-08-18)

## 동결 지점
- 태그: `wm-v32-rc0-speed-baseline` (Pre-release로 게시)
- branch: perf/wm-v32-remaining-production-readiness
- commit: 894fff7a27 ("docs(v32): Phase 8~10 완료")
- Modal app: inbilab-wm-gpu-v32-speed-staging (workspace jangis89)
- 마지막 배포 workflow run: #46 (final-fix-deploy, 성공)
- handler: gpu-worker/handler_v32.py (frame_count_fast + S3 multipart + 스트리밍 finish + 폴링 예외방어)

## 실행 구성 (용어 명확화 — 명세 A.2)
| 항목 | 값 | 의미 |
|---|---|---|
| permanent_min_containers | **0** | 사용자 없을 때 켜져 있는 GPU 없음 (24시간 상시 아님) |
| request_time_prewarm | **3** | 작업 접수 후 scan과 병렬로 warm_v32 3개 발사 |
| true standby | 없음 | 미사용 |
| buffer_containers | 0 | 미사용 |
| K (세그먼트 수) | 12 | |
| key_step | 5 | |
| hedge_delay | 120초 (권장, 벤치 드라이버 구현) | 스트래글러 보험, first-valid-wins |
| active_gpu_jobs | 1 (FIFO 티켓제) + finish overlap | |
| S3 multipart | 16MB × 8동시, staging 키(Modal Secret v32-staging-s3)만 | 실패 시 단일 PUT 폴백 |
| validator | packet count(frame_count_fast) + 크기/해상도/FPS/오디오 | ※ fault-injection 검증은 Phase D에서 |
| max_containers | 32 | |
| GPU | L40S, cpu8/mem64G, scaledown 300s | |
| 이미지 | pytorch 2.7.1-cuda12.8 + minimax-remover /models | |

## 동결 시점 성능·품질 (원자료: docs/*)
- 한산 P50 162.6s (5회: 142/153/163/168/283), finish tail P50 27.5s
- request-time prewarm 3 후보: P50 169.8s
- 완전 콜드 첫 작업: +2~3분 (시간대 변동 37~195s 배정지연)
- 품질: PSNR 36.90/SSIM 0.9808/경계 0, 골든 육안 8/10 (박스자막 1, 골든설계결함 1)

## 이 RC의 공표된 미해결 차단요인 (명세 1장)
박스자막 자동제거 / 골든 15개 / validator fault-injection / 최종 공식 재측정 /
경쟁사 실속도 비교 / 실제 원가 / rollback dry-run / production key 미발급
