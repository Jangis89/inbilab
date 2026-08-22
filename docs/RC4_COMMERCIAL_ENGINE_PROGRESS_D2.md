# RC4 상업 엔진 단계 진행 로그 — 2일차 갱신 (2026-08-22)

브랜치: `perf/wm-v32-rc4-commercial-engine` · 후속 명세 P1~P5 + P6 v1 완료 시점 기록

## 완료 (전부 실측)
- **P6 D/F 하이브리드 11/11 ROI 완료** — 골든 5 + UAT 6 전 구간 cand_D/cand_F
  생성·업로드 (여러 차례의 인프라·구현 문제를 넘김):
  - v1 OOM: 합성이 seg 프레임 전체를 후보별 RAM 복사 → UAT 해상도에서 16GB
    러너 강제종료 3회 재현 → **v2 스트리밍 합성**으로 해결
  - v3: uat02(조각 54개, 최대 824s/조각)가 순차로는 러너 5h 한도 초과 확실 →
    **생성 호출 병렬 window=6** (품질·GPU초 동일). 대표 협의 원칙 반영:
    한도는 분할·병렬로 해결, 폭주 방지(300분)는 유지
  - v3.1: 저장소 일시 오류(HTTP 520) 대비 업/다운로드 3회 재시도
- **5후보 동일창 지표 55행** (`RC4_HYBRID_DF_TOURNAMENT.csv`) + 6패널 시트
  11 ROI 44점 재생성 (fail=0)
- **P5 GPU flow 벤치 완료** (`RC4_GPU_FLOW_BENCHMARK.csv`, H100!/H200 엄격
  분리 32행): **SEA-RAFT 왕복오차 압도적 최저**(0.07~0.37px; DIS는 uat03에서
  16.6px로 사실상 무효). 품질 지표는 두 GPU에서 동일(결정적), 속도만 미세 차이.
  DIS 0.03-0.04s/flow vs SEA-RAFT 0.07-0.18s/flow — "일반=DIS, 구조=GPU flow"
  라우팅의 실측 근거 확보.
- svor 워커 신판 배포: MUSE 전처리 스위치 + flowbench op + SEA-RAFT 이미지.
- P4 MUSE A/B 6건(g27/g33/uat02_t92_112 × C/E) 실행 중.

## P6 v1의 핵심 발견 (정직 기록 — 다음 이터레이션 방향)
- D/F v1은 residual을 **12프레임 chunk 단위**로 생성모델에 전달 → 시간 문맥
  부족으로 **골든 PSNR이 C/E보다 낮고**(g33 D 11.88), uat01_t154 육안에서
  D=녹색 글자형 잔상, F=흐릿한 잔상 (E 전체창 방식은 다리 복원 유지).
- 반면 **속도 구조는 실증**: 조각당 8~67s (전체창 236~242s 대비) + UAT 일부
  구간에서 flicker 최저(D: t92_112 8.66 vs E 11.9 / F: t154 4.18 vs E 8.24).
- 결론: **residual 생성의 시간 창을 chunk(12f)→part 창(60~81f)으로 병합**하는
  P6 v2가 필요 — 같은 (part,reg)의 연속 chunk 조각을 하나로 합쳐 1회 생성.
  E의 품질과 D/F의 속도·안정성을 결합하는 다음 단계.
- flow_bypass가 UAT 자막에도 지배적(정지 위치 오버레이 특성) —
  coverage 실측 3~17%. 실화소 전파는 이동 오버레이(transient)에서 유효하고,
  정지 자막·카드는 un-blend+생성 경로가 주력임이 데이터로 확정.

## 알려진 미해결
- LPIPS 열 미산출(라이브러리 로드 문제 추정) — PROVE RC-S/RC-T(P7)로 대체 우선.
- A(cand_A)의 out_core 최대값은 별도 인코딩 체인 잡음 — D/F는 동일 체인로 2~5.

## 다음
1. P6 v2: chunk 병합 residual 생성 → D/F 재실행 → 재비교
2. MUSE A/B 결과 분석 (`RC4_MUSE_AB_REPORT.md`)
3. P7 PROVE 통합 → 후보 압축(≤2)
4. 이후 P8 속도최적화 → P11~
