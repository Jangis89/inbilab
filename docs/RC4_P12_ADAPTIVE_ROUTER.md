# RC4 Phase 12 — Adaptive Router · 자동 품질 재처리 설계

- 작성: 2026-08-23 02:2x KST · 근거 명세 §12 · 선정 근거 `RC4_ENGINE_SELECTION_DECISION.md`
- 상태: **설계 확정 + 임계값 초안**. 구현·계측은 §13/§15와 함께 진행.

---

## 12.1 경로 정의 (명세 §12.1 매핑)

| 명세 | 이름 | 구성 | 언제 |
|---|---|---|---|
| A | fast | 생성 없음, 기존 실화소 처리 | 자막 없음/극소, 저난이도 |
| B | flow(DIS) | DIS half flow 실화소 전파 | 일반 이동 오버레이 |
| C | flow(GPU) | SEA-RAFT flow 전파 | 사람·동물·격자 등 구조 장면 |
| D | **quality** | flow → residual hole → **SVOR-1.3B (=후보 F)** | 일반 자막 잔여 |
| E | **heavy** | **SVOR-1.3B 전체창 (=후보 E)** | **불투명 카드 자막**, quality 실패 |
| F | review/manual | 사람 확인 | 위 전부 실패 |

**flow 엔진 선택 근거(P5 실측)**: SEA-RAFT 왕복오차 0.072~0.371px로 전 구간 최저,
DIS는 uat03에서 16.59px로 사실상 무효. 반면 DIS가 0.025~0.043s/flow로 3~5배 빠름.
→ **일반은 DIS, 구조 장면은 SEA-RAFT** (명세 §5.5와 일치).

---

## 12.2 난이도 신호 → 경로 (임계값 초안)

측정 가능한 신호만 사용한다. 괄호 안은 이 값을 얻는 기존 구성요소.

| 신호 | 출처 | 라우팅 규칙(초안) |
|---|---|---|
| `mask_area_pct` | scan/segment 마스크 | < 0.2% → fast |
| `card_score` | **un-blend 단계의 카드 검출**(RC4-16/17) | 카드 검출 시 → **heavy(E)** 강제 |
| `real_pixel_coverage` | flow 전파 실측 | ≥ 85% → flow만으로 종료 |
| `residual_hole_ratio` | flow 후 잔여 구멍 비율 | > 15% → quality(F) |
| `human/animal overlap` | 세그먼트 마스크 교차 | 있음 → flow는 SEA-RAFT 사용 |
| `grid/line score` | 에지 방향 히스토그램 | 높음 → SEA-RAFT + quality |
| `camera motion` | flow 평균 크기 | 큼 → SEA-RAFT |
| `duration` | 입력 | 길수록 heavy 비중 억제(예산 가드) |

**핵심 규칙(실측 근거)**: 카드가 검출되면 **처음부터 heavy(E)** 로 보낸다.
F로 먼저 시도하면 card_res 0.93으로 실패가 확정적이므로 GPU를 두 번 쓰게 된다.

---

## 12.3 처리 후 자동 품질검사 (게이트)

각 경로 출력에 대해 **같은 업로드 안에서** 자동 측정:

| 검사 | 도구 | 불합격 임계(초안) |
|---|---|---|
| 글자잔상 | `glyph_residual_v32` glyph_ratio | 구간 중앙값 대비 +50% 초과 |
| 카드 잔존 | 동 card_res | **> 0.35** (E 실측 0.246 통과 / F 0.932 불합격) |
| 공간 자연스러움 | PROVE RC-S | 동일 영상 내 다른 구간 대비 이상치 |
| 시간축 | PROVE RC-T | 동상 |
| mask 밖 오염 | out_core maxdiff | **> 10** (생성계 실측 2~5) |
| 색조·halo | 기존 지표 | 기존 기준 유지 |

> 임계값은 **초안**이다. §13에서 골든/UAT 실측 분포를 보고 확정하며,
> **§14 blind holdout을 연 뒤에는 임계값을 수정하지 않는다**(명세 §14.3 오염방지).

## 12.4 자동 승격 사다리

```
fast 실패 → flow(DIS)
flow 실패 → flow(SEA-RAFT)
flow 실패 → quality(F)
quality 실패 → heavy(E)
heavy 실패 → review/manual
```
- **같은 업로드 안에서 승격**한다. 재업로드 요구 금지(명세 §12.4).
- 승격 시 이미 계산한 mask·flow·실화소 결과를 재사용해 중복 GPU 비용을 막는다.

## 12.5 목표 사용률과 현재 예상

| 경로 | 명세 목표 | 현재 데이터 기반 예상 | 비고 |
|---|---|---|---|
| fast + flow | ≥ 70% | 미측정 | §15에서 실제 분포 측정 필요 |
| quality(F) | ≤ 30~40% | 자막 있는 영상 대부분 | 창당 $0.023 |
| **heavy(E)** | **≤ 10%** | **카드 자막 비율에 종속** | 창당 $0.26 — 초과 시 원가 급증 |
| manual | ≤ 5% | g28형 격자 구간 | 현재 해법 없음 |

**위험**: 카드형 자막이 고객 영상에서 10%를 넘으면 heavy 사용률 목표를 초과한다.
→ 완화책 우선순위: (1) 카드 un-blend 강화로 F가 카드를 처리하게 만들기,
(2) 카드 구간만 잘라 heavy에 넣기(이미 국소 적용), (3) heavy 예산 상한 후 manual.

---

## 12.6 구현 순서 (다음 작업)

1. `handler_v32`에 라우터 함수 추가 — 신호 계산은 기존 scan/segment 산출물 재사용
2. 자동 품질검사 훅: `glyph_residual`·PROVE를 파이프라인 내부 호출로 통합
3. 승격 사다리 상태머신 + 중복 계산 방지 캐시
4. §13 골든/UAT 전체 처리로 임계값 확정 → **동결**
5. §14 blind holdout 개봉

**대표 승인 전 production 반영 없음** — staging 경로에서만 구현·검증한다.
