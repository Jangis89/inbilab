# RC4 사고 기록

## INC-3: UAT 원본 3편 스토리지 소실 (발견 2026-08-20 ~21:05Z)

### 원인
1. 운영의 원본 정리 기능(24h 정책)이 UAT 원본 source 파일 3개를 삭제했다.
   - u2/u3: INC-2(RC3) 후 같은 경로로 재업로드했으나, 행의 `cleaned_at`이 이미
     찍혀 있어 이후 정리 사이클에서 다시 삭제된 것으로 판단 (13:39Z 이후~21:05Z 사이).
   - u1(bad96c77): 행은 `failed_wm`/`cleaned_at=null`이라 예외로 믿었으나 파일이
     없다. UAT 복제 행(beac0005-…, status=wm_done)이 **같은 source_path를 공유**
     하므로, 복제 행 기준으로 정리가 동작하면 원본 파일이 지워진다(구조적 원인).
2. 작업 컨테이너가 세션 사이에 재생성되어 로컬 사본(src1/2/3.mp4)도 소실됐다.
   두 보존처가 동시에 사라져 복구 불가 상태가 됐다.

### 영향
- RC4 Phase M(UAT 3편 재처리 + Source/RC2/RC3/RC4 비교)이 **원본 재확보 전까지 불가**.
- RC2/RC3 출력물은 무사(스토리지 잔존 확인, 해시 동결, bench-assets/uat-preserve/
  보존 완료 — `RC4_BASELINE_RC3_OUTPUTS_SHA256.md`).
- 다른 Phase(A~L: holdout 동결, Locator/전파/복원 구현, 골든 검증)는 영향 없음
  — 골든·holdout은 bench-assets 마스터로 만들며 이 영역은 정리 비대상.

### 해결책
1. **(추천) 대표가 UAT 원본 3편을 사이트에 재업로드** — 새 프로젝트로 올리면
   내가 SHA256 대조(동결값 32958d0e…/974e4cd9…/9be1239a…)로 동일 파일임을 검증
   후, 즉시 `bench-assets/uat-preserve/src1~3.mp4`로 보존 복사한다. 이후 UAT
   실행은 보존 사본을 쓰도록 해 재발을 차단한다.
2. 대표 PC/휴대폰에 원본이 없으면: blind holdout 20종+골든으로 품질 판정을
   진행하고, UAT 비교는 RC2/RC3 출력 간 비교로 대체(원본 열 없이). 판정력 저하.
3. (보조) 정리 정책 코드 수정으로 UAT 경로 예외 추가 — 운영 코드 변경이라
   대표 승인 필요, RC4 범위 밖 권고사항으로만 기록.

### 재발 방지 (즉시 적용)
- 기준물은 발견 즉시 `bench-assets/`(정리 비대상)로 서버측 복사하는
  `preserve` 워크플로 액션 신설 (RC4 브랜치, run #144 검증 완료).
- 이후 UAT 복제 행의 source_path는 보존 사본 경로를 가리키게 하여 운영 정리와
  완전히 분리할 계획 (uat_run_v32.py 수정 — 원본 재확보 후 적용).

## INC-4: g02 골든 세그먼트 900s 행 (진행 중, 2026-08-20~21)

### 실측 타임라인 (확인된 사실만)
- run #147: g02~g05 "timeout of 900s" 취소. 이후 체인+예산(RC4-3),
  세그 공유 deadline 90s(RC4-4) 적용에도 재발.
- A/B 확정: `WM_RC4_FLOW=0`이면 g02 **100.2s 통과(37.79dB)** → flow 경로 관련.
- rc4dbg 계측(RC4-5, cv2.setNumThreads(0)+ocl off 적용 후에도 동일):
  - part1: probe_done(cover 0.026) → **bypass_ai_start 직후 무응답**
  - part2: probe_done(cover 0.081) → **bypass_ai_start 직후 무응답**
  - part3: chain 전파 정상 완료(25fr당 ~6.5s) → **hole_ai_start 직후 무응답**
  - part0: crop_hq(h29.restore_chunk, flow 이전)는 정상 완료 — 즉 AI 호출
    자체는 flow 실행 **전**에는 정상, flow 실행 **후**에는 행.
- "timeout of 900s"는 segment(1800s)가 아니라 finish_v32_cpu(900s)의 함수
  타임아웃 — 세그 행 대기 중 finish 클록이 먼저 소진되는 구조.

### 배제된 가설
- MiniMax scale/steps 문제 (flow=0에서 정상)
- flow 연산 자체의 무한루프 (chain_fr 진행 로그로 배제)
- cv2 전역 스레드풀-torch 충돌 (setNumThreads(0)+ocl off로 미해결 — RC4-5)

### 현재 조치 (RC4-6, 커밋 4100cd4)
- 세그 감시 스레드: 240s/480s 시점 전 스레드 스택 덤프 → rc4dbg 채널
  (덤프 부재 = GIL 쥔 네이티브 행이라는 진단 정보)
- 전 dbg 이벤트에 RSS(메모리) 텔레메트리 — 메모리 스래싱 가설 판별
- ai_enter/ai_exit 이벤트 (전달 프레임 shape/개수 포함)
- 검증 run: 32441263616 (goldenrun only:g02)

### 비용 (정직 보고용, 누계)
- 행 재현 run당 GPU L40S 4대×~15분 + 러너 ~16분. run #147/#151/#157/#161/#163
  + 금회 = 진단 사이클 6회. 별도: run #153이 러너에 2h29m 걸려 있던 것을
  발견 즉시 취소(2026-08-21).


### INC-3 해결 (2026-08-21 04:0xZ 확인됨)
- 대표가 사이트에 3편 재업로드 (sc_projects: 842d2be5/8f2d2669/7825dfd4, status=uploaded).
- preserve 액션(run #170)으로 videos-source→videos-clips/bench-assets/uat-preserve/
  교차버킷 보존 복사 + 재해시 (RC4-9에서 bucket:path 스펙 지원 추가).
- SHA256 대조: src1=32958d0e…(159,459,560B) / src2=974e4cd9…(159,536,565B) /
  src3=9be1239a…(119,951,400B) — **동결값과 3/3 완전 일치 (확인됨)**.
- Phase M(UAT 재처리·4-way 비교) 차단 해제. 업로드 행 3건은 처리 미시작 상태로 유지
  (원본은 이미 보존됨 — 운영 정리가 videos-source 사본을 지워도 무영향).

### INC-4 해결 (2026-08-21, 확인됨)
- **원인 확정 (수정-검증 A/B)**: rc4 flow 경로(bypass/hole)가 h29.restore_chunk에
  넘기는 마스크 리스트에 **청크 내부 None 항목**이 남음. rc4의 크롭 래퍼가
  "빈 마스크 프레임"을 None으로 재구성하는데, h29.restore_chunk는 청크 범위
  마스크가 전부 ndarray라고 가정(np.stack). flow=0 경로는 원본 마스크(전부
  ndarray)를 그대로 넘겨 문제가 없었다.
- **관측된 증상**: 함수 실행 스레드가 예외 보고 없이 소멸(스택 덤프에 부재),
  Modal 메인 루프는 결과 대기 → finish(900s) 타임아웃. cv2 스레딩 가설은 기각.
- **수정 (RC4-8)**: _ai_fb에서 None→zeros 정화 + BaseException 로깅 유지.
- **검증**: run #169 goldenrun only:g02 → **PASS** (total 121.4s, 마스크영역
  PSNR 31.81→37.79dB, quality_pass, 600/600 프레임). flow probe는 정지배경
  상시자막을 정확히 bypass(cover 0.026/0.081), 팬 구간은 chain 전파 수행.
- 계측 인프라(rc4dbg 스토리지 채널·감시 스택덤프·RSS)는 이후 디버깅 자산으로
  브랜치에 유지.
