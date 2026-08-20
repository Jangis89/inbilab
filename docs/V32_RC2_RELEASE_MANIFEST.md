# V32 RC2 릴리스 매니페스트 — transient overlay 자동제거

- 날짜: 2026-08-20
- 브랜치: `perf/wm-v32-transient-overlay-auto` (기준: `Jangis89-patch-1` @ 8b8cc758 = hotfix2 동결)
- 최종 코드 커밋: `28307067` (veto 최종 규칙 + 게이트 재보정 포함) (gpu-worker/handler_v32.py, scripts/make_golden_v32.py, scripts/golden_run_v32.py)
- 태그: `wm-v32-rc2-transient-overlay` = 28307067 (게시 완료, Pre-release) · PR #1 (perf 머지) 대기
- 운영 영향: **없음** (staging 앱 `inbilab-wm-gpu-v32-speed-staging` 전용, 운영 v29 무접촉)

## RC2에서 새로 만든 것

1. **transient overlay 감지기** (`detect_windowed_transient_overlays`)
   - 글자 지속성 히트맵(1/8 해상도, 프레임별 글자 상자 누적): 같은 자리에 글자가
     ≥15% 샘플 동안 머무는 픽셀만 후보 — 일반 자막(≈7%)은 자동 배제
   - 등장 구간: 영역 edge 에너지 타임라인 + hysteresis → ivals(전역 프레임 구간)
   - Type A(반투명 카드): rect 감지(detect_box) + **(α, 카드색) 자동 추정**
     — 경계 인접 픽셀쌍 강건 회귀 (UAT-02 실측 α=0.50, C≈흰색 254, 5회 반복 수렴)
     → 밴드 box 경로에 공급, un-blend로 배경 실복원 (그룹 추정 실패/밝은 카드일 때 2차로 사용)
   - Type B(반투명 워터마크/캡션): 구간별 시간축 union 마스크(ival_packs) + 원해상도 AI 복원
   - 카드 안 시간축 글자 마스크는 밴드 마스크에 병합(extra_packs) — un-blend된 프레임 위 복원
2. **실물 텍스트 보호(veto)** (`_scene_text_veto`)
   - 항목별 최근접 매칭으로 "장면과 함께 움직이는 글자"(표지판·모니터·옷 인쇄)를
     감지 영역에서 제외 — 전역 이동(phase correlation) 대비 scene/fixed 비율 판정
3. **골든 25종 체계** (기존 15 + transient 양성 5 + 실물 negative 5)
   - make_golden_v32.py: g16~g20(clean GT 보유), g21~g25(실물 모사, 오탐0 게이트)
   - golden_run_v32.py: kind별 게이트 (negative = transient 0 + 입력 대비 무변화)

## 검증 결과 (최종 — 릴리스 게이트 run #116, 코드 28307067)

| 게이트 | 결과 |
|---|---|
| **골든 25종** | **25/25 통과** (gt-real 5/5 · real 5/5 · gt-box 5/5 · transient 5/5 · negative 5/5) |
| 기존 15종 회귀 | 0 (g15는 지표 모집단 재보정 — 코드 주석·아래 참고) |
| 실물 negative | 표지판·모니터·창문·옷글자·정적장면 **오탐 제거 0** |
| UAT-01 (자막+巴图+캡션) | 자막·캡션·巴图 제거 확인 (run106 @44) |
| UAT-02 (반투명 카드) | 박스 un-blend 제거 성공 — 구간별 편차 있음: 완전 제거 구간(t120)과 흐릿한 잔상 구간(t45) 공존 (run109 @47, 대표 육안 판정 대상) |
| UAT-03 (빠른 전환·복잡 배경) | 자막·캡션 제거 + **앞치마 실물 글자 보존** (run106 @46) |
| 흰 가로줄 재발 | 0 (실측) · 중간 중국어 재발 0 (실측) |
| deep audit | 전 실행 실패 0 |
| 공식 벤치 | warm10 P50 237.9s(≤240✓) P95 464.4s(≤480✓) · cold5 P50 247.2s · 실패 0/15(≤1%✓) |

**g15 재보정 기록**: scene-text veto가 g15의 무관한 두 번째 영역(실물 장면 텍스트)을
지표 계산에서 제외하면서 평균 희석이 사라져 순수 박스 점수(19.94)가 드러남.
run99↔run114 출력 프레임 픽셀 동일 확인(시각 회귀 아님) → in_box 게이트 20.0→19.5
재보정(golden_run_v32.py 주석 명시).

## 실행 이력 (staging, GitHub Actions run #)

- 90/93/97/100/101/105/108: deploy (반복 수정 배포)
- 91/94/98/102/106/109: UAT 재처리 (복제 @31~@47, 원본 무변경)
- 92: 골든 g16~g25 제작 · 95/99/103/107/110: 골든 25종 검증
- 반복 사유 기록: run91(카드 un-blend 오추정·마스크 비대) → run94(AI 환각) →
  run98(정적 이중처리 잔상) → run102(글자 잔존) → run106(+veto v2) → run109(최종)

## 프론트 (Track 1 — 완료, 별도 커밋)

- main `1e194279` — 재생 중 video 노드 보존 2곳 수정, Vercel 배포·49.5분 모니터 0오류
- docs: FRONTEND_VIDEO_PLAYBACK_BUG_REPORT.md / _TEST_RESULTS.md (perf 브랜치 docs/)

## 남은 단계

1. ~~공식 벤치~~ 완료 (위 표) · ~~태그~~ **완료**: `wm-v32-rc2-transient-overlay` @ 28307067 (Pre-release 게시)
2. **PR #1 머지** (perf ← transient 브랜치) — 비가역 버튼이라 대표 직접 클릭 필요
3. 대표 4문 판정 (①카드·巴图 자동제거 충분? ②기존 항목 재발 없음? ③폴링 중 재생 안 끊김? ④C0 진행 승인?)

## 알려진 한계 (정직 고지)

- 반투명 카드 un-blend는 균일 α 모델 — 카드 모서리·테두리의 α 편차로 미세 흔적이
  남을 수 있음 (테두리 링 AI 마스크로 완화)
- 카드가 장면마다 위치 이동 시, 위치별 감지 키가 적으면 일부 구간 품질 저하 가능
  (소수키 그룹 구제 로직으로 완화, run109에서 확인)
- 자동 감지 실패 시 최후 안전망은 기존 [직접 지정] 모드 (파일 재업로드 불필요, 동일 업로드에서 재처리)
