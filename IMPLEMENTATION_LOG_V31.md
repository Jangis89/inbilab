# IMPLEMENTATION_LOG_V31 — V31 초고속 파이프라인 실행 기록

측정·판단 근거를 남기는 실행 일지. 모든 수치는 실측(로그·GitHub Actions·Modal 대시보드)이며,
추정치는 "추정"으로 명시한다. 기준 영상: 감사 기준 3분 영상(31118dec 원본 참조,
벤치 전용 행 beac0001-0000-4000-8000-000000000031, auto/fast, N=5,320프레임).

## 단계 0 — 현상 고정 (완료)
- HEAD == 감사 커밋 c5f8ee0, diff 0 (CURRENT_HEAD_DIFF_FROM_AUDIT.md)
- v29 Modal 기준 실측: 총 1,175s (plan 478s: masks 310 / mask_dec 80 / scan 42 / dl 34;
  workpool×5: dec 291–320 / ai 61–124 / enc_up 24–32; mergeseg×5: chunks 62–98 / comp 39–87; finish 22)
  → PERF_BASELINE_V31.md

## 단계 1–2 — 구현·배포·동일성 게이트 (완료)
- handler_v31.py: 정확 구간 해독(-ss (f0-0.5)/fps, -vframes n, 개수 검증),
  통합 segment worker(AI+즉시합성+단일 인코딩, 중간 o_*.mp4 제거= intermediate_ai_mp4_count 0),
  v29 함수 재사용(restore_chunk/ownership/합성식 동일), wmtmp-v31 prefix 격리
- modal_app_v31.py: 앱 inbilab-wm-gpu-v31-staging (운영과 완전 분리),
  CPU plan/finish + L40S segment/warm 자원 분리
- 배포: GitHub Actions run #3, 39s 성공
- 동일성 게이트: run #2 통과 ✅ — 순차 vs 구간 해독 100+프레임 RGB byte 동일,
  v29 crop 스트림 vs v31 구간 crop 10영역×8프레임 byte 동일
- crop exact=1 A/B: run #2 로그 기준 — 결과는 로그 참조(불일치 시 기존 방식 유지 방침)

## 단계 3 — 벤치 A/B (진행 중)
### 스모크 run (Actions #4, 2026-08-17 KST, label=smoke, K=5, warm=5)
- 결과: OK — **total 1,249.5s** (t_plan_done 735.8 / t_segments_done 1,113.2 / finish +136.3)
- v29 기준 1,175s보다 **+74.5s 느림**. 원인 분석:
  1. **plan 735.8s (v29 478s 대비 +258s)** — CPU 이미지에 스레드 1개 제한
     (OMP_NUM_THREADS=1 등 + cv2.setNumThreads(1))을 걸어 16코어 중 1코어만 사용한 실수. [확인된 설정, 영향치는 추정]
  2. **warm 설계 결함** — warm_v31_gpu(별도 함수)를 데워도 segment_v31_gpu 컨테이너 풀은
     데워지지 않음(Modal은 함수별 풀). 게다가 plan(12분)과 동시에 데워 scaledown으로 식음.
     → 스모크의 segment 377.4s에는 GPU 냉시동+모델 적재가 포함됨.
- 조치(코드 수정 완료, 커밋 대기):
  - cpu_image 스레드 제한 제거, plan/finish의 cv2.setNumThreads(1) 제거
  - 예열을 segment_v31_gpu 자신에게 {phase:'warm_v31'}로 보내고, plan 완료 직후 실행,
    segment_v31_gpu에 scaledown_window=300 부여
  - 벤치 로그에 run 레코드 전체(JSON) 출력([REC]) — 단계별 tms 관측 확보
  - verify_v31.py 신설(action=verify): 프레임 수==5,320·오디오·해상도 하드 게이트 +
    v29 결과 대비 PSNR/SSIM 리포트
- 중단 요인: 사장님 브라우저 GitHub 로그인이 쓰기 권한 없는 다른 계정(jangis89-Growthchain)으로
  바뀌어 커밋·배포 불가. Jangis89 재로그인 대기 중 (2026-08-17 04:5x KST).

## 다음 측정 계획 (승인된 스테이징 범위)
1. 수정본 배포 → verify(품질 하드 게이트) → warm 벤치 runs=3 (K=5)
2. cold 벤치 runs=1–2 → 단계3 게이트(P50 ≤ 480s) 판정
3. 게이트 미달 시 단계 4(plan 병렬화: 다중 프로세스/공유 메모리) 착수 — 현 병목 1순위가 plan이므로
   스레드 해제 후에도 P50 초과 시 곧바로 진행

## 2026-08-17 낮 — 단계 4 반복 (측정→수정→재측정)
- (사고 복구) 컨테이너 초기화로 로컬 작업본 소실 → GitHub 원본에서 재구성. GitHub·Modal·Supabase
  재로그인은 사장님이 수행.
- run #9 (stage4a: NPROC 30, cpu=32, warm=segment 풀·plan 후): 1160/1388/1676 — masks 466~578
  불변 → 프로세스 수는 병목 아님. finish.verify 54→13s 개선 확인. warm 대기 15~143s 손실.
- run #10 (K=10, warm=0): 1115/1382. 세그 구간 289s(좋을 때). AI 부하 세그별 36~124s 불균등 관찰.
- 단계4b: _par_sweep_shm(공유메모리) 구현 — 로컬 합성영상 동일성 테스트 byte 일치 통과 후 배포.
- run #12 (shm, K=10, warm=5): 987/1297/1243. run0 plan 500s(masks 371, scan 42) vs
  run1 plan 882s(masks 680, scan 118) — **같은 코드로 1.8배 편차 = CPU 전용 워커 하드웨어 복불복이
  지배 변수**. shm 자체는 부정적 아님(최고 기록 987s 갱신)이나 결정타 아님.
- 단계4c: plan_v31_gpu 추가 — v29가 검증한 GPU 상자(빠르고 균일한 CPU)에서 plan 실행하는 A/B.
  WM_NPROC=14(GPU box), benchmark --plan_on, workflow plan_on 입력. run #14로 측정 중.
- run #15 (best-k12, plan=cpu, warm=5): 1389/1494/1256 — masks 593~686(나쁜 상자 연속),
  K=12 이득 소멸. 설정 튜닝 한계 확정 → PERF_V31_STAGE4_REPORT.md 에 게이트 판정·결정 요청 정리.
- 산출물: BENCHMARK_V31_all.json (14 runs 통합), PERF_V31_STAGE4_REPORT.md.
