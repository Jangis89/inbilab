# V32 RC4 시작 상태 (Phase A 동결 기록)

- 날짜: 2026-08-20
- 목적: RC4(SOTA 하이브리드 복원) 작업 시작 시점의 상태를 고정 기록한다.
- 유효 명세: `CLAUDE_V32_RC4_SOTA_HYBRID_RESTORATION_FINAL_SPEC_REV2.md` (이전 RC4 명세 전면 대체)

## 기준선 (rollback 지점)

| 항목 | 값 | 확인 방법 |
|---|---|---|
| base 브랜치 | `perf/wm-v32-rc3-restoration-quality` | branches 페이지 (2026-08-20) |
| base 코드 커밋 | `1261b042a3fae1bf27ef8d49c94e1ed4edeea6cf` (RC3 최종) | atom feed 실측 |
| rollback 태그 | `wm-v32-rc3-restoration-quality` = 1261b042 (Pre-release 게시 확인) | release 페이지 |
| RC4 작업 브랜치 | `perf/wm-v32-rc4-sota-hybrid` — 1261b042에서 분기 생성 (2026-08-20) | 생성 직후 atom feed tip=1261b042 실측 |
| PR #1 | **Open 유지 확인** (2026-08-20, branches 페이지 "Open pull request #1") — merge/close/force-push 금지 | branches 페이지 |
| Modal 앱 | `inbilab-wm-gpu-v32-speed-staging` (staging 전용) · 운영 v29 무접촉 | — |
| 운영 상태 | v29 유지 · V32 일반 트래픽 0% · permanent GPU 0 · production S3 key 미발급 | RC3와 동일 |
| 프론트 | main `1e194279` — 대표 통과 판정, RC4에서 재개발 금지 | RC3 기록 승계 |

## RC3 대표 판정 (RC4의 출발점)

| UAT | 판정 | 사유 |
|---|---|---|
| UAT-03 | **통과 (상용 목표품질)** | 새 영상에서 반복되면 개발 종료 가능 수준 — RC4에서 fast path 품질 회귀 금지 |
| UAT-02 | 불통과 | 카드 영역·가장자리 얼룩, 불투명 직사각형 흔적, t=5 카드 밖 불투명화, t=150 붉은 오염 — "카드가 원래 없었던 영상"이 목표 |
| UAT-01 | 불통과 | t=154 아이 다리를 흐리게 지움 — 사람·동물·기와·격자·바닥·반복무늬의 실제 구조·질감 복원 필요 |
| 총평 | 한 영상 성공은 통과 아님 | blind holdout 20종에서 반복되어야 함 |
| 전략 제약 | MiniMax scale/steps/마스크 미세조정을 주전략 금지 | 아키텍처 교체(실화소 우선 하이브리드)로 해결 |

## 기준물 SHA256 (동결)

### UAT 원본 (삭제 금지 — RC3 START_STATE에서 승계, 원본 문서 커밋으로 동결됨)

| UAT | 프로젝트 ID | source SHA256 |
|---|---|---|
| UAT-01 | bad96c77-b609-4eaf-9de6-cfddc07e4c18 | `32958d0e918f5bed859ed040d5d6ccd3db959ace0e9cde5c8300fc3e68a71be6` |
| UAT-02 | d5a9ae7f-3960-4914-b048-b3953a3d5245 | `974e4cd9bec29c708de3e7478a73764291637d9705eedaa2e2fda621b5e5da59` |
| UAT-03 | 5ddc260c-8519-4ec1-9f41-6fdef71031b8 | `9be1239a7358a6671b4988aad5d9ac8c138cd428782a6a87c84023b3ea36ae7e` |

### RC2 최종 출력 (비교 기준 — RC3 START_STATE에서 승계)

| UAT | output SHA256 |
|---|---|
| UAT-01 | `960ba22aaed87be63bfaddd81032d1475a11fb864044f8f4697fe9645343faf5` |
| UAT-02 | `5b3b3b63bbc4641c3c8e7800f3273616a53b67b0e4eed32459d9b2604d919358` |
| UAT-03 | `44f44da117d9e5634a013bd2700bf886a1298278653962cb44bba6c1eba0b5ad` |

### RC4 기준 코드 (RC4 브랜치 tip 1261b042에서 raw 취득 후 실측)

| 파일 | SHA256 |
|---|---|
| gpu-worker/handler_v32.py | `d3d6aa312ebe005c2fb567b425f84bfeecfee3d8da211eb81b34eff7c60c1e1c` |
| gpu-worker/scripts/uat_run_v32.py | `d6db78fc0737d7a319161751e21b2a34dd26e381e64ef2e142106b5485b0b5a0` |
| gpu-worker/scripts/make_golden_v32.py | `c4dfeca443d70ae03bd6b79cdd96ad40eadad5360dc4bc933c88a4c0aecf217d` |
| gpu-worker/scripts/golden_run_v32.py | `b8dbee571b2c84d471ae9e9013dbac0e77d756a0132f36146dfa07ac98f30617` |
| gpu-worker/scripts/evaluate_restoration_quality.py | `25283651b5420b2a86deac7dd3691a2e9845909a250682611bed624ef708fdc5` |
| docs/V32_RC3_START_STATE.md | `3c06ccb434612e3c74992d7a0dffe49f352d3a24e71dcf2df358a5b545b6f8be` |
| docs/V32_RC3_RELEASE_MANIFEST.md | `9088918611970b8abb8bd12c6b9443990a913bf92e378ecc0028630ef6c079b8` |
| docs/RESTORATION_QUALITY_BASELINE_RC2.csv | `15a733bb375bc46dfd522f58a36909c99c5494c9c519c99768db217e52a2235c` |

### 정직 고지 — RC3 출력물 해시

작업 컨테이너가 세션 사이에 재생성되어 RC3 로컬 사본(UAT 출력 rc3h_o1/o2/o3,
프레임 추출본, 분석 산출물)이 소실되었다. RC3 출력 영상 자체는 staging 스토리지의
복제 프로젝트(beac0005-…)에 남아 있으며, RC4 Phase M 비교자료 준비 시점에
**가장 먼저 재다운로드하여 SHA256을 `docs/RC4_BASELINE_RC3_OUTPUTS_SHA256.md`로
동결**한다(스토리지 정리 정책 리스크 대비 조기 수행). RC3 결과의 성적·지표는
커밋된 `V32_RC3_UAT_REPORT.md`·`V32_RC3_RELEASE_MANIFEST.md`가 원본 기록이다.

## RC3 공식 성적 (승계 기준)

- 골든 29/35 (기존 25/25 유지·오탐 0, 신규 복원 4/10)
- fast warm10 P50 204.3s / P95 211.2s / 실패 0 · cold5 P50 212.4s
- overlay 경로: u1 541.7s / u2 579.5s / u3 272s · 실패 0/18
- 원가: 일반 ≈$0.6~0.7 · 고품질 $1.2~2.6

## RC4 성공 기준 (명세 REV2 요약)

1. blind holdout 20종(개발 전 동결): A+B ≥ 18, C ≤ 2, **D = 0**
2. 필수 게이트: UAT-01 t154 사람 다리 보존, UAT-02 t5 카드 밖 불투명화 0 ·
   t150 붉은 오염 0 · 카드 모양 잔존 0
3. detection 25/25 유지 + restoration 골든 ≥ 9/10
4. 라우팅: fast+flow ≥ 80%, heavy ≤ 20%, manual ≤ 5%
5. 속도: fast P50≤240/P95≤480 · flow P95≤600 · heavy P95≤900 목표 · 실패 ≤1%
6. 비상업 라이선스 컴포넌트 production 채택 금지 (라이선스 매트릭스 필수)
7. 고객 영상 무단 학습 금지 (refiner 학습은 합성 데이터만)

## 금지 사항 (대표 승인 전 — RC3에서 승계)

PR #1(또는 대체 PR) merge · production S3 key 발급 · production C0/C1 ·
일반 고객 V32 전환 · 테스트 데이터 삭제 · 경쟁사 유료 결제
