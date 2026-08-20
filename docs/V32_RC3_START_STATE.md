# V32 RC3 시작 상태 (Phase A 동결 기록)

- 날짜: 2026-08-20
- 목적: RC3(잔상·얼룩·복원 품질) 작업 시작 시점의 상태를 고정 기록한다.

## 기준선 (rollback 지점)

| 항목 | 값 |
|---|---|
| base 브랜치 | `perf/wm-v32-transient-overlay-auto` |
| base 코드 커밋 | `28307067` (RC2 최종 코드) |
| base 문서 커밋(tip) | `fe9aa43c` (docs만 추가 — 코드 동일) |
| rollback 태그 | `wm-v32-rc2-transient-overlay` = 28307067 (Pre-release 게시됨, 확인) |
| PR #1 | **Open 유지 확인** (2026-08-20) — merge/close/force-push 금지 |
| RC3 작업 브랜치 | `perf/wm-v32-rc3-restoration-quality` (fe9aa43c에서 분기) |
| Modal 앱 | `inbilab-wm-gpu-v32-speed-staging` (staging 전용) · 운영 v29 무접촉 |
| 운영 상태 | v29 유지 · V32 일반 트래픽 0% · permanent GPU 0 · production S3 key 미발급 |
| 프론트 | main `1e194279` 재생 끊김 수정 — 대표 판정 통과, 이번 작업에서 재개발 금지 |

## UAT 원본 (삭제 금지, SHA256)

| UAT | 프로젝트 ID | source SHA256 |
|---|---|---|
| UAT-01 (자막+巴图+캡션, 175.0s 1080x1920@30 hevc) | bad96c77-b609-4eaf-9de6-cfddc07e4c18 | `32958d0e918f5bed859ed040d5d6ccd3db959ace0e9cde5c8300fc3e68a71be6` |
| UAT-02 (반투명 카드, 175.0s) | d5a9ae7f-3960-4914-b048-b3953a3d5245 | `974e4cd9bec29c708de3e7478a73764291637d9705eedaa2e2fda621b5e5da59` |
| UAT-03 (빠른 전환·복잡 배경, 130.1s) | 5ddc260c-8519-4ec1-9f41-6fdef71031b8 | `9be1239a7358a6671b4988aad5d9ac8c138cd428782a6a87c84023b3ea36ae7e` |

## RC2 최종 출력 (비교 기준, SHA256)

| UAT | RC2 실행 | output SHA256 |
|---|---|---|
| UAT-01 | run106 @beac0005-…44 | `960ba22aaed87be63bfaddd81032d1475a11fb864044f8f4697fe9645343faf5` |
| UAT-02 | run109 @beac0005-…47 | `5b3b3b63bbc4641c3c8e7800f3273616a53b67b0e4eed32459d9b2604d919358` |
| UAT-03 | run106 @beac0005-…46 | `44f44da117d9e5634a013bd2700bf886a1298278653962cb44bba6c1eba0b5ad` |

## 현재 처리 설정 (RC2 시점)

- 처리 tier: `fast` = scale 0.5 / steps 4 (UAT 3편 모두 fast로 처리됨)
  - 참고 tier: std = 0.75/6, hq = 1.0/8 (handler.py TIERS — RC3 quality/heavy fallback 후보)
- UAT-02 카드 un-blend 추정값: α=0.5003 (균일 가정), 카드색 C≈254(흰색) — 경계 인접 픽셀쌍 강건 회귀
- 카드 처리 방식: 밴드 box 경로에 card_blends 공급, 카드 내 글자 마스크는 extra_packs로 밴드에 병합
- transient interval: edge 에너지 타임라인 + hysteresis(0.55/0.35) — **UAT-02 카드는 사실상 상시 존재로 확인됨** (RC2 중 baseline_intervals 부정확 판명)
- 실물 텍스트 보호(veto): 항목별 최근접 매칭, 최종 규칙 (scene≥15 & fixed==0) OR (scene≥8 & tot≥40 & ratio≥0.7)

## 현재 성능 (RC2 공식 벤치)

- 일반 fast path: warm10 P50 237.9s / P95 464.4s · cold5 P50 247.2s · 실패 0/15
- scan 단계 28~34s (transient 감지 추가분 ≈0~5s)

## 알려진 품질 결함 (대표 불통과 판정, RC3 대상)

1. **UAT-02**: 글자만 사라지고 반투명 카드 배경·잔상 미제거 — 절대 불통과 (가장 심각)
2. **UAT-01**: 지운 자리 얼룩 다수, 그래픽·질감 복원 부정확
3. **UAT-03**: 상대적으로 양호하나 지운 부근 얼룩 시인됨
4. RC2 증거 프레임: 카드 t15(잔영)/t45(유령 잔상)/t120(완전 제거)/t100(박스 잔영) — 구간별 편차

## RC3 성공 기준 (재정의)

"글자가 없어졌는가"가 아니라 **"정상속도(1×) 재생에서 지운 자리가 원래부터 자연스러웠던 것처럼 보이는가"**.
자동지표는 방향/회귀 탐지용이며 대표 육안 판정이 최종 gate.

## 금지 사항 (대표 승인 전)

PR #1(또는 대체 PR) merge · production S3 key 발급 · C0/C1 · 일반 고객 V32 전환 · 테스트 데이터 삭제
