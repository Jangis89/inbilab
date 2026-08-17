# V32_STRAGGLER_ANALYSIS — worker 지각 분포 재분석 (Phase 0.3)

데이터: 보존된 V32 벤치 22 runs / 252 segments (2026-08-18 재구성).
원자료: V32_WORKER_ALLOCATION_DISTRIBUTION.csv (row = segment).

## 측정 가능/불가능
기존 로그에는 worker_request_at / container_assigned_at / model_ready_at 이 없다 (미측정).
따라서 개별 worker의 배정 대기시간은 산출 불가하고, 아래 하한(lower bound)만 산출했다:
```
run_alloc_tail_lower_bound = (세그 전체 wall) - (최대 세그 exec)
```
이 값이 0이면 "가장 늦게 끝난 세그의 배정 대기 ≈ 0"이고, 크면 최소 1개 worker가
그만큼 배정(또는 재배정)을 기다렸다는 뜻이다. **몇 개가 늦었는지는 미측정** —
이번에 추가한 계측(아래)부터 정확히 수집한다.

## 핵심 발견 (실측)
1. **한산 시간대(11/22 runs): tail ≈ 0.** 12개 전부 즉시 배정. 배정 문제 자체가 없음.
2. **혼잡 시간대 지각은 두 종류다:**
   - **배정 지각**: exec는 정상(126~155s)인데 wall이 커짐. tail 하한 100~427s.
     (final-warm10 run4: tail 427.4s, run5~9: 100~180s — 혼잡창 내내 지속적 2~3분 배정 지연)
   - **실행 지각**: 세그 exec 자체가 628s/685s (run2, run3) — 정상(~140s)의 4~5배.
     느린/스로틀된 GPU 또는 자원 경합으로 추정(원인 미측정). **배정만 고쳐서는 안 잡힘.**
3. cold_model 수(모델 적재>1s인 세그 수)는 warm=12 요청에도 0~11로 널뜀 —
   Modal warm이 혼잡 시 유지되지 않는 경우가 있음.

## 대표 가설("보통 3개만 늦는다")에 대한 현재 답
- 지각 worker '개수' 분포는 기존 로그로 판정 불가 (미측정) — 계측 후 재답변.
- 다만 두 시사점은 이미 확실하다:
  ① 혼잡창에서는 배정 지연이 특정 1개가 아니라 **여러 run에 걸쳐 지속**됐다 (공급 부족형 존재).
  ② **실행 지각(4~5배 느린 실행)이 실존**하므로, 명세 2.2의 지시대로 warm 3개를
     세그먼트 고정 배정이 아닌 **공용 큐 + work stealing + delayed hedge +
     first-valid-wins**로 써야 두 종류 지각을 모두 흡수할 수 있다.
     (고정 배정이면 실행 지각 1건이 전체를 인질로 잡는다 — run2/3이 그 실증)

## 이번에 추가한 계측 (Phase 0.2)
- segment_v32 반환에 `container_id`, `t_enter`(함수 시작 절대시각), `t_done` 추가
- 벤치가 세그별 `dispatch_at` 기록 + 완료 폴링으로 `done_at` 기록
  → `alloc_wait = t_enter - dispatch_at`, `exec = t_done - t_enter` 를 worker별로 산출
- 이후 모든 벤치에서 late_count_15/30/60/120/300s, P50/P90/P95 worker_wait 자동 집계 가능
