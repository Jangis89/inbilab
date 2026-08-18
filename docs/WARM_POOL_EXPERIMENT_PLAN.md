# V32 warm/dedicated/hedge 통제실험 계획 (Phase 8)

원칙 (사장님 지시 그대로):
- 개발 중 permanent warm pool 금지. 실험은 구성당 최대 2시간의 짧은 통제실험.
- 실험 종료 즉시 Modal min_containers=0 / buffer_containers=0, RunPod workersMin=0 원복 확인.
- 24시간 상시 GPU는 상용화 직전 대표 최종 승인 전 활성화 금지.
- warm 3개 가설은 세그먼트를 특정 worker에 고정 배정하지 않는다 —
  공용 segment queue + work stealing + delayed hedge + first-valid-result-wins로 검증.

## 구현 방식 (Modal 위에서)
- 공용 큐/work stealing: Modal 함수 호출 큐가 이미 "가용 컨테이너가 아무 세그나 집어가는"
  공용 큐다 (spawn된 호출은 특정 컨테이너에 고정되지 않음). 즉 고정배정 금지는 기본 충족.
- delayed hedge + first-valid-wins: 벤치 드라이버에 구현 완료 (benchmark_v32.py --hedge N초).
  발사 후 N초 지나도 미완료인 세그는 복제 발사, 먼저 도착한 유효 결과 채택.
  세그 출력은 같은 키에 동일 내용이 저장되므로 중복 실행이 결과를 오염시키지 않음.
- true standby vs base worker 구분: warm_v32 예열(모델 로드)된 컨테이너 수 vs
  scaledown_window(300s) 안에 남아 있는 직전 실행 컨테이너 수를 container_id 계측으로 구분.

## 실험 매트릭스 (구성당 벤치 3~5회, 총 2시간 이내씩)
| ID | warm | hedge_s | 목적 |
|---|---|---|---|
| A0 | 0 | 0 | 완전 콜드 기준선 |
| A1 | 1 | 0 | 최소 warm 효과 |
| A2 | 3 | 0 | warm 3 가설 본검증 |
| A3 | 3 | 60 | warm 3 + 보수적 hedge |
| A4 | 3 | 30 | warm 3 + 공격적 hedge |
| A5 | 12 | 0 | 현행 최대 warm 기준선 (기존 데이터 재사용) |
| A6 | 12 | 30 | 혼잡 P95 개선 검증 (burst 9 조건) |
| A7 | 0 | 30 | hedge 단독 효과 |
| A8 | 3 | 15 | hedge 민감도 |

판정 기준: 구성별 P50/P95(반복 기준), hedge 발사율(비용 증가율), straggler 제거율.
warm3 가설 통과 조건: A2~A4 중 하나가 "P50 ≤ warm12 P50 + 10%, P95 ≤ 480s"를
비용(작업당 GPU초) 20%+ 절감으로 달성.

## 원복 체크리스트 (각 실험 후)
1. modal_app_v32.py에 min_containers/buffer_containers 파라미터를 넣지 않는다
   (warm은 warm_v32 예열 호출로만 — 컨테이너는 scaledown 300s 후 자동 소멸).
2. 실험 후 Modal 대시보드 apps → inbilab-wm-gpu-v32-speed-staging → 컨테이너 0 확인.
3. RunPod: 현재 비활성 (workersMin=0 유지) — 2차 공급자 비교는 별도 짧은 실험으로.
