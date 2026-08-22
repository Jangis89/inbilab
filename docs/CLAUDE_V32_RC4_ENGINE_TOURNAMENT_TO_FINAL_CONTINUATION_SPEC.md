# 인비랩 자막·워터마크 제거기 V32 RC4
# 상업용 복원엔진 토너먼트 완료 → 실화소 하이브리드 → UAT/Vmake/Blind 최종검증 실행 명세서
## 문서 버전: 2026-08-21 CONTINUATION-FINAL
## 대상: Claude Cowork / Claude Desktop
## 기준 보고서: `RC4_상업엔진_단계_상세보고서.md`
## 기존 유효 명세: `CLAUDE_V32_RC4_COMMERCIAL_ENGINE_SVOR_VACE_FINAL_SPEC.md`
## 현재 브랜치: `perf/wm-v32-rc4-commercial-engine`
## 현재 기준: RC4-18 승계 + RC4-19 mask_export + SVOR/VACE 토너먼트 하네스
## 운영 상태: v29 정상 / 일반 고객 V32 트래픽 0% / production S3 미발급 / permanent GPU 0
## PR #1: Open 유지 / 대표 승인 전 머지 금지
## 목적: 기존 명세를 처음부터 반복하지 않고, 현재 진행 중인 1차 라운드를 회수한 뒤 RC4의 남은 Phase를 실제로 끝낸다

> **문서 성격**
>
> 이 문서는 새 방향을 다시 기획하는 문서가 아니다.
> Claude가 이미 완료한 G0·G1·G2·G3 하네스·G6 SVOR 앱·UAT 원본 보존을 그대로 승계하여,
> **진행 중인 엔진 토너먼트부터 최종 RC4 상용 후보 확정까지 이어가는 후속 실행 명세서**다.
>
> 현재 보고서 작성 시점에는:
>
> ```text
> 11개 ROI × VACE-1.3B / SVOR-1.3B 비교 중
> g26 1개만 첫 결과 확인
> ```
>
> 상태였다.
>
> 따라서 “전체 작업 완료”가 아니다.
> 진행 중 run의 결과를 먼저 회수하고, 실패한 job만 재실행한 뒤 아래 순서대로 계속한다.
>
> 대표의 최우선 원칙:
>
> ```text
> 1. 품질
> 2. 새 영상에서도 반복되는 안정성
> 3. 처리속도
> 4. 개발 완료속도
> 5. 비용
> ```
>
> 비용을 아끼기 위해 품질·속도·개발속도를 희생하지 마라.
> 그러나 비싼 GPU나 최신 모델을 사용했다는 사실만으로 채택하지 않는다.
>
> 최종 목표:
>
> ```text
> - UAT-01·UAT-02에서 Vmake와 동등 이상 품질
> - UAT-03 품질 회귀 없음
> - 새 blind 영상에서도 같은 수준 반복
> - fast 경로 속도 유지
> - 품질 경로가 상용 가능한 시간 안에 끝남
> - production에 비상업 모델 포함 0
> ```

---

# 0. 현재 authoritative 상태

## 0.1 이미 완료돼 반복하면 안 되는 작업

```text
G0:
상업엔진 브랜치 생성 및 시작상태 동결

G1:
MiniMax 상업 라이선스 문의 실제 발송
2026-08-21 19:05:46 KST
Message-ID 기록 완료

G2:
후보 12종 라이선스 매트릭스 v2
모델 SBOM
가중치 SHA256
SVOR/VACE commercial chain GREEN 확인

안전조치:
UAT 원본 3편 bench-assets/uat-src/ 보존
SHA256 3/3 일치

G6 일부:
별도 Modal 앱 inbilab-wm-svor-staging
SVOR/VACE-1.3B weights 적재
H100!/H200 BF16 smoke
가중치 5/5 SHA256 통과

G3:
동일 입력·동일 동결 mask 토너먼트 하네스
ROI 팩 11개
mask_export
contact sheet
metrics CSV

g26 첫 비교:
현재 엔진보다 VACE·SVOR 질감·카드 제거가 개선되는 첫 증거
```

위 작업을 다시 만들거나 처음부터 반복하지 마라.

## 0.2 현재 진행 중이던 작업

```text
전 11개 ROI × 후보 C/E
C = 순정 VACE-1.3B
E = SVOR-1.3B

남은 실행:
20 jobs
```

보고서 작성 당시 실행 중이었다.

## 0.3 아직 하지 않은 핵심

```text
전 11 ROI 결과 수합·공정한 동일창 비교
flow + VACE residual
flow + SVOR residual
MUSE mask A/B
GPU RAFT/SEA-RAFT
VACE-14B quality ceiling
PROVE RC-S/RC-T
최종 엔진 선정
production hybrid 통합
adaptive router
자동 품질 재처리
UAT 3편 최종 처리
Vmake 직접 비교
golden 최종
blind holdout 20종 첫 평가
최종 P50/P95/원가
production에서 MiniMax 제거 또는 상업허가 반영
RC4 tag
대표 최종 판정
```

## 0.4 첫 결과의 정확한 의미

g26 첫 81-frame window:

```text
현 엔진:
mask PSNR 15.53
sharpness 0.866

VACE:
mask PSNR 16.63
sharpness 0.997
flicker ratio 0.825

SVOR:
mask PSNR 15.58
sharpness 0.977
flicker ratio 0.890
```

현재 한 구간에서는 VACE가 수치상 앞섰다.

그러나:

```text
- 현재 엔진은 600 frames
- VACE/SVOR는 81 frames
- 10개 다른 ROI 결과 없음
```

이므로 최종 승자라고 선언하지 마라.

## 0.5 속도 위험

SVOR smoke:

```text
81 frames
약 2.7초 영상
720×1280
20 steps

H100 inference:
239.1초

H200 inference:
230.6초
```

현재 raw full-ROI SVOR는 production 속도로 불합격이다.

따라서 최종 production 후보는 원칙적으로:

```text
real-pixel propagation
→ residual hole 최소화
→ 작은 crop/window만 VACE/SVOR
```

구조여야 한다.

---

# 1. 전체 실행 순서

```text
Phase 1  진행 중 20건 결과 회수·실패 job만 재실행
Phase 2  동일 81-frame window 기준 정식 C/E 토너먼트
Phase 3  Vmake 비교자료 통합
Phase 4  MUSE mask A/B
Phase 5  GPU flow A/B와 실화소 coverage 강화
Phase 6  후보 D/F: flow + residual VACE/SVOR
Phase 7  속도 최적화 전 품질 우승후보 압축
Phase 8  필요 시 VACE-14B quality ceiling
Phase 9  필요 시 구조형 specialist
Phase 10 최종 상업엔진 선정
Phase 11 production hybrid path 통합
Phase 12 adaptive router·자동 품질검사
Phase 13 golden/UAT/Vmake 개발세트 최종검증
Phase 14 blind holdout 20종 첫 평가
Phase 15 최종 속도·원가·안정성
Phase 16 MiniMax 라이선스 처리·production package
Phase 17 RC4 tag·대표 판정
```

---

# Phase 1 — 진행 중 20건 회수

## 1.1 중복실행 금지

먼저 기존 GitHub Actions와 저장소 자산을 확인한다.

```text
이미 완료:
결과 회수

진행 중:
기다려 완료 확인

실패:
실패한 ROI/후보만 재실행

취소:
취소 사유 확인 후 해당 job만 재실행
```

전 20건을 무조건 새로 발사하지 않는다.

## 1.2 각 job 필수 확인

```text
ROI id
candidate C/E
input SHA256
mask SHA256
window start/end
frames
GPU exact type
cold/warm
queue wait
model load
inference
encode
upload
success/failure
output SHA256
```

## 1.3 incomplete result

다음은 실패 처리:

```text
81 frame 미만
해상도 불일치
duration 불일치
mask mismatch
checksum 불일치
silent corrupt
```

## 1.4 산출

```text
docs/RC4_ROUND1_CE_RAW_RUNS.csv
```

---

# Phase 2 — 공정한 정식 C/E 토너먼트

## 2.1 동일 창 비교

현재 엔진 A도 C/E와 동일한:

```text
동일 81 frames
동일 source
동일 mask
동일 crop/context
동일 output
```

으로 다시 잘라 비교한다.

600-frame A와 81-frame C/E를 한 표의 절대값으로 섞지 않는다.

## 2.2 ROI 목록

```text
g26
g27
g28
g31
g33

uat01_t100_115
uat01_t154

uat02_t5
uat02_t92_112
uat02_t143_161

uat03_t45_60
```

## 2.3 후보

```text
A:
현재 RC4 엔진의 동일창 출력

C:
순정 VACE-1.3B

E:
SVOR-1.3B
```

## 2.4 지표

```text
mask ROI PSNR
SSIM
LPIPS
sharpness ratio
edge continuity
high-frequency ratio
residual glyph
residual card
color cast
halo
flicker
outside-mask identity
runtime
VRAM
cost
```

PROVE가 아직 없으면 현재 지표로 먼저 정식표를 만들고
Phase 7 전 PROVE로 재평가한다.

## 2.5 육안

각 ROI:

```text
Source
A
C
E
가능하면 GT
```

동일 frame contact sheet와
정상속도 나란히 clip.

## 2.6 판정

한 ROI의 숫자 하나가 아니라:

```text
글자·카드 제거
사람·동물 구조
선·격자
질감
시간축
mask 밖 보존
```

을 함께 본다.

산출:

```text
docs/RC4_ROUND1_CE_TOURNAMENT.csv
docs/RC4_ROUND1_CE_VISUAL_REPORT.md
evidence/round1_ce/
```

---

# Phase 3 — Vmake 비교자료 통합

대표가 확보한 파일:

```text
1번 (브이메이커).mp4
2번 (브이메이커).mp4
```

## 3.1 파일 확보

현재 Claude 대화나 대표 PC에서 파일을 찾는다.

없으면 대표에게 두 파일 업로드만 요청한다.

이 파일 부재 때문에 다른 개발을 멈추지 않는다.

## 3.2 대응

```text
1번 Vmake:
UAT-01

2번 Vmake:
UAT-02
```

원본과 duration·resolution·FPS를 확인한다.

## 3.3 동일 구간

```text
UAT-01:
t100~115
t154
필요 시 t20~25

UAT-02:
t5
t92~112
t143~161
t150
```

## 3.4 비교

```text
Source
Vmake
A
C
E
후속 D/F
```

이름을 가린 contact sheet와 clip을 만들 수 있도록
파일 구조를 준비한다.

---

# Phase 4 — MUSE mask A/B

## 4.1 현재 mask 유지

기존 mask를 baseline으로 둔다.

## 4.2 MUSE

VAE temporal compression window에 맞춰:

```text
anchor mask
이후 4-frame 그룹 temporal OR
복원/반복
```

A/B.

## 4.3 대상

```text
g27
g33
camera motion/outline
UAT-02 glyph ghost
```

## 4.4 검사

```text
mask 누락 감소
글자 유령 감소
flicker 감소
과확장
실물 오삭제
원가
```

과확장이 실제 물체를 지우면 탈락.

산출:

```text
docs/RC4_MUSE_AB_REPORT.md
```

---

# Phase 5 — GPU flow 강화

## 5.1 후보

```text
DIS half
torchvision RAFT small
SEA-RAFT
```

SEA-RAFT weights·학습데이터·상업조건을 다시 확인한다.

불명확하면 production GREEN으로 올리지 않는다.

## 5.2 exact GPU

```text
H100!
H200
```

벤치 결과가 섞이지 않게 엄격히 지정.

## 5.3 대상 장면

```text
UAT-01 t154 사람 다리
g28 격자
g31 반복패턴
UAT-03 no-regression
```

## 5.4 지표

```text
forward-backward error
occlusion
valid coverage
wrong-pixel ghost
real_pixel_coverage
residual_hole_ratio
runtime/frame
GPU memory
```

## 5.5 선택

```text
일반:
DIS half

사람·동물·격자·구조:
GPU flow
```

가 유력하나 실측으로 확정.

잘못된 flow로 실제 구조를 겹쳐 붙이는 것보다
residual hole이 큰 편이 낫다.

산출:

```text
docs/RC4_GPU_FLOW_BENCHMARK.csv
```

---

# Phase 6 — 후보 D/F 하이브리드

이 Phase가 이번 다음 작업의 핵심이다.

## 6.1 D

```text
flow real-pixel propagation
→ residual hole
→ VACE-1.3B
```

## 6.2 F

```text
flow real-pixel propagation
→ residual hole
→ SVOR-1.3B
```

## 6.3 crop 최소화

생성엔진 입력은:

```text
residual hole bbox
+ 구조·motion context margin
```

만 사용.

전체 자막 rectangle을 다시 생성하지 않는다.

## 6.4 Preserver

모든 출력:

```text
effect mask 밖 원본 강제 보존
```

유지.

feather 영역도 별도 계측하여
mask 밖 최대변형이 큰 값으로 오해되지 않게:

```text
core outside
feather ring
```

을 분리해 기록한다.

## 6.5 카드

```text
un-blend
→ glyph/card residual mask
→ flow texture reinforcement
→ residual VACE/SVOR
```

카드 전체를 생성엔진으로 다시 그리지 않는다.

## 6.6 구조

```text
사람 다리
동물
격자
기와
반복패턴
```

은 실제 화소가 충분하면 생성엔진 mask에서 제외.

## 6.7 비교

```text
A/C/E/D/F
```

5후보 정식표.

산출:

```text
docs/RC4_HYBRID_DF_TOURNAMENT.csv
evidence/hybrid_df/
```

---

# Phase 7 — PROVE와 품질 우승후보 압축

## 7.1 PROVE

공식 PROVE를 별도 evaluator에 통합한다.

```text
RC-S:
공간적 제거 자연스러움

RC-T:
시간축 제거 일관성
```

의존·weights·license를 SBOM과 대조.

## 7.2 후보 압축

11개 ROI에서:

```text
대표 육안 품질
구조 보존
RC-S
RC-T
residual
halo
outside-mask
```

기준으로 2개 이하 후보로 줄인다.

## 7.3 속도는 탈락선만 우선 적용

품질후보 선별 중:

```text
P95 900초 초과 예상
OOM
실패율
```

이면 탈락.

아직 품질후보를 낮추기 위한 양자화는 하지 않는다.

---

# Phase 8 — 속도최적화

품질 우승후보에만 적용한다.

## 8.1 후보

```text
FlashAttention 호환
SDPA vs FlashAttention
torch compile
model residency
VAE/T5 cache
fixed/static shapes
window parallelism
batching
crop/pad 최적화
overlap 8/12/16
step 20 baseline vs 16/12
```

## 8.2 원칙

steps 감소는:

```text
품질 동등
```

이 증명될 때만.

## 8.3 SVOR full-ROI 금지선

현재 raw SVOR:

```text
81 frames ≈230초
```

따라서 full-ROI 기본 path로 사용하지 않는다.

생성엔진은 residual/crop/window 중심이어야 한다.

## 8.4 속도 게이트

품질 path 목표:

```text
P50 ≤360초
P95 ≤600초
```

heavy fallback:

```text
P95 ≤900초
사용률 ≤10%
```

---

# Phase 9 — VACE-14B quality ceiling

다음 조건에서만 실행:

```text
VACE/SVOR 1.3B 하이브리드가
UAT-01/02 또는 g28/g31에서 품질 미달
```

## 9.1 smoke

순서:

```text
H200 1장
B200 1장 호환
필요 시 2GPU
```

BF16 무양자화 먼저.

## 9.2 범위

문제 ROI와 residual hole만.

전체 영상 기본경로 금지.

## 9.3 채택

VACE-14B가:

```text
대표 육안에서 명확히 개선
heavy 사용률 낮음
P95≤900
```

일 때만 heavy fallback.

---

# Phase 10 — 구조형 specialist 조건부 비교

다음 조건에서만:

```text
g28 격자
g31 반복패턴
```

이 최종 후보에서도 실패.

후보:

```text
FGT
ISVI
```

가중치·dependency 라이선스 GREEN 확인 후
ROI 국소 비교.

불명확하면 실행하지 않는다.

---

# Phase 11 — 최종 엔진 선정

## 11.1 선정 우선순위

```text
1. 대표 육안 품질
2. 사람·동물·구조 보존
3. 시간축 안정성
4. 공간적 자연스러움
5. 새 개발세트 일반화
6. 처리속도
7. 실패율
8. 비용
```

## 11.2 필수 탈락

```text
읽을 수 있는 글자잔상
카드 ghost
사람·동물 구조손상
격자·선의 심각한 단절
심각 flicker
mask 밖 오염
비상업 라이선스
P95 >900
```

## 11.3 엔진 결정문

```text
docs/RC4_ENGINE_SELECTION_DECISION.md
```

내용:

```text
왜 선택했는지
왜 탈락했는지
어떤 장면에 어떤 엔진인지
라이선스
속도
원가
known limitations
```

---

# Phase 12 — Adaptive Router·자동 재처리

## 12.1 경로 후보

```text
A:
fast

B:
DIS flow

C:
GPU flow

D:
flow + selected 1.3B residual

E:
VACE-14B heavy

F:
review/manual
```

## 12.2 난이도 신호

```text
mask area
duration
card
human/animal overlap
grid/line
texture
camera motion
flow confidence
real_pixel_coverage
residual_hole_ratio
```

## 12.3 처리 후 품질검사

```text
RC-S
RC-T
residual glyph
card opacity
blur
texture
edge
halo
color cast
outside-mask
```

## 12.4 자동 승격

```text
fast 실패
→ flow

flow 실패
→ quality residual

quality 실패
→ heavy

heavy 실패
→ review/manual
```

같은 업로드에서 실행.
재업로드 요구 금지.

## 12.5 목표 사용률

```text
fast + flow ≥70%
quality ≤30~40%
heavy ≤10%
manual/review ≤5%
```

모든 영상을 quality/heavy로 보내면 실패.

---

# Phase 13 — 개발세트 최종검증

## 13.1 기존 감지

```text
25/25
```

유지.

## 13.2 남은 실패

```text
g26
g27
g28
g31
g33
```

목표:

```text
5/5 통과
```

## 13.3 UAT

```text
UAT-01:
t100~115
t154

UAT-02:
t5
t92~112
t143~161
t150

UAT-03:
t45~60
```

## 13.4 Vmake

```text
Source
Vmake
RC3
최종 RC4
```

이름을 가린 비교 clip/contact sheet.

대표 선택:

```text
인비랩 우세
동등
Vmake 우세
둘 다 불가
```

## 13.5 UAT 목표

```text
UAT-01:
사람 다리·질감 Vmake 동등 이상

UAT-02:
카드·글자유령·flicker Vmake 동등 이상

UAT-03:
RC3 품질 유지
```

---

# Phase 14 — Blind Holdout 20종 첫 평가

최종 candidate·router·threshold를 동결한 뒤 처음 연다.

## 14.1 등급

```text
A:
UAT-03 수준

B:
약간 차이는 있으나 실사용 가능

C:
정상 재생에서 얼룩·blur·색조가 명확

D:
사람·물체·구조손상 또는 심각 오탐
```

## 14.2 합격

```text
A+B ≥18/20
C ≤2
D =0

사람·동물:
심각 구조손상 0

카드:
5/5 실사용 가능

실물 오삭제:
0
```

## 14.3 오염방지

첫 결과 후 동일 holdout을 보며 threshold 수정 금지.

수정 필요 시:

```text
기존 holdout 결과는 실패로 보존
새 holdout 세트 생성
```

---

# Phase 15 — 최종 성능·원가·안정성

## 15.1 fast

```text
warm10
cold5

P50 ≤240
P95 ≤480
```

## 15.2 quality

최소 10건:

```text
P50 ≤360 목표
P95 ≤600
```

## 15.3 heavy

최소 5건 또는 충분한 표본:

```text
P95 ≤900
사용률 ≤10%
```

## 15.4 기록

```text
queue wait
cold start
model load
flow
generation
finish
audit
total
VRAM
GPU utilization
cost
```

## 15.5 실패율

```text
≤1%
```

## 15.6 permanent

```text
0
```

---

# Phase 16 — MiniMax 라이선스·Production 정리

## 16.1 답변 확인

MiniMax 메일:

```text
2026-08-26 KST까지
```

답변 확인.

답변이 오면:

```text
상업 허용범위
SaaS
output
modification
fine-tune
fee
royalty
term
territory
attribution
```

을 문서화.

법률판단이 필요하면 대표에게 원문과 요약만 제공.

## 16.2 무응답

3영업일 무응답:

```text
follow-up 초안
```

작성.

대표 확인 후 1회만 발송.

개발은 계속.

## 16.3 미허가

MiniMax:

```text
production code path 제거
production image/volume 제거
research-only 격리
SBOM RED
```

## 16.4 허가

허가를 받았어도:

```text
엔진 토너먼트 승자
```

일 때만 채택.

---

# Phase 17 — Release·대표 최종판정

## 17.1 tag

```text
wm-v32-rc4-commercial-hybrid
```

## 17.2 PR

대표 승인 전 머지 금지.

최종 commit history를 보고:

```text
PR #1 확장
또는
clean integration PR
```

을 제안.

## 17.3 final manifest

```text
docs/V32_RC4_COMMERCIAL_RELEASE_MANIFEST.md
```

포함:

```text
selected engine
license
weight SHA
router
quality gates
golden
UAT
Vmake
blind
P50/P95
cost
known limits
production state
PR state
```

## 17.4 대표 판정 질문

```text
1. UAT-01이 Vmake 이상인가?
2. UAT-02가 Vmake 이상인가?
3. UAT-03 품질이 유지됐는가?
4. blind 20개 일반화가 통과했는가?
5. 속도가 고객이 받아들일 수준인가?
6. 상업 라이선스가 정리됐는가?
7. 실제 활성 사용자를 V32 기본엔진으로 전환할 준비를 시작해도 되는가?
8. PR merge를 승인하는가?
```

---

# 2. 중간보고 규칙

다음 이정표에서만 보고한다.

```text
1. C/E 20건 정식 결과
2. D/F hybrid 결과
3. VACE/SVOR 품질후보 결정
4. VACE-14B 필요 여부
5. UAT/Vmake 비교 준비
6. blind holdout 결과
7. 최종 성능·원가
8. 라이선스 답변
9. 운영 위험
```

각 보고:

```text
완료
원자료
품질
속도
실패
비용
남은 병목
다음 실행
```

을 포함.

---

# 3. Claude가 멈추고 대표에게 물어볼 경우

```text
- Vmake 2파일이 없어서 대표 업로드가 필요할 때
- MiniMax follow-up 실제 발송 승인
- 신규 카드·유료 장기계약
- 예상보다 큰 외부 GPU 비용
- production secret
- 운영 고객 트래픽
- PR merge
- 되돌리기 어려운 production 변경
```

그 외 staging 기술결정은 실측으로 진행.

---

# 4. 최종 완료조건

```text
[ ] C/E 11 ROI 정식 동일창 비교 완료
[ ] D/F hybrid 비교 완료
[ ] MUSE A/B 완료
[ ] GPU flow 비교 완료
[ ] PROVE 통합 완료
[ ] 상업 production 엔진 선정

[ ] detection 25/25
[ ] g26/g27/g28/g31/g33 5/5
[ ] UAT-01 대표 통과
[ ] UAT-02 대표 통과
[ ] UAT-03 no-regression
[ ] Vmake 동등 이상 또는 대표 종합선택

[ ] blind A+B ≥18/20
[ ] blind D=0
[ ] 실제 물체 오삭제 0
[ ] 심각 flicker/halo/card ghost/outside 오염 0

[ ] fast P50≤240
[ ] fast P95≤480
[ ] quality P95≤600
[ ] heavy P95≤900
[ ] failure≤1%
[ ] permanent GPU 0

[ ] production 모델 license GREEN
[ ] 비상업 weights production 포함 0
[ ] final tag/manifest
[ ] 대표 판정자료
```

---

# 5. 마지막 원칙

```text
현재 중간보고를 전체 완료로 오인하지 않는다.

현재 진행 중 20건을 중복 발사하지 않는다.

g26 한 구간만 보고 엔진을 정하지 않는다.

SVOR가 최신 전용 모델이라는 이유로 자동 승자로 만들지 않는다.

VACE가 g26에서 앞섰다는 이유로 즉시 기본엔진으로 만들지 않는다.

raw full-ROI SVOR의 230초/81frame 속도를 숨기지 않는다.

실제 화소로 채울 수 있는 부분을 먼저 채운다.

생성모델은 residual hole에 집중한다.

Vmake는 완벽한 GT가 아니라 상용 최소 benchmark다.

Blind holdout을 마지막까지 봉인한다.

대표 육안품질과 새 영상 일반화가 최종 gate다.

대표 승인 전 production·PR merge를 실행하지 않는다.
```
