# RC4 Phase D — flow 엔진 비교 벤치 (run #149, 2026-08-21)

방법: benchmark_master 파생 GT 3케이스(팬/구간카드/실모션+상시자막),
프레임당 단발 전파(오프셋 ±2/8/20, 체이닝·2pass 없음)로 엔진만 분리 비교.
지표는 "전파가 수용한 화소"의 PSNR과 커버리지 — 파이프라인 전체 성능이 아니라
엔진 간 상대 비교용. 수치는 `RC4_FLOW_ENGINE_BENCHMARK.csv`.

## 결론

1. **dis_half(현행 채택 유지)** — 0.3~0.4s/frame(CPU)로 가장 빠르면서 팬 30.9dB로
   farneback·dis_full보다 정확. 반해상 추정이 안티앨리어싱 효과를 내
   원해상(dis_full 27.7dB)보다 오히려 낫다(실측).
2. **RAFT 계열은 카메라 이동에서 +6.8dB** (37.7 vs 30.9) — 명세 D의 기대대로
   학습기반 flow의 우위가 카메라 모션 케이스에서 확인됨. 단 CPU 8~27s/frame로
   CPU 채택 불가. **GPU(L40S, segment worker에 torch 상주)에서는 ~0.1s/frame
   수준이므로 heavy 경로 한정 GPU-RAFT 옵션이 유효** — Phase G/H에서
   카메라이동+고위험 마스크 조합에 A/B.
3. raft_large는 small 대비 이득 미미(팬 −0.5dB, 이동 +4dB) — 채택 시 small 우선.
4. SEA-RAFT(BSD-3, spec 우선 후보): torchvision RAFT로 상한을 근사 측정했다.
   GPU-RAFT A/B에서 이득이 확정되면 SEA-RAFT 가중치 통합(공식 배포 채널 확인
   포함)을 진행한다. 라이선스는 둘 다 BSD-3로 production 적합.

## 라이선스 (매트릭스 연동)

dis/farneback: OpenCV Apache-2.0 · RAFT: torchvision BSD-3(가중치 포함) ·
SEA-RAFT: BSD-3. 전부 상업 사용 가능.
