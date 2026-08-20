# RC4 SOTA 리서치 · 라이선스 매트릭스 (v1, 2026-08-20)

원칙(명세 REV2 §5): **비상업 라이선스 컴포넌트는 production 채택 금지.**
staging 실험은 허용하되, 최종 파이프라인에 남는 모든 컴포넌트는 상업 사용이
법적으로 명확해야 한다. 아래 각 행은 1차 실사(공식 리포/HF 모델카드) 결과다.

## 판정 요약표

| 컴포넌트 | 용도 후보 | 코드 라이선스 | 가중치 라이선스 | 상업 사용 | RC4 판정 |
|---|---|---|---|---|---|
| **SEA-RAFT** (princeton-vl) | flow 엔진 (E) | BSD-3-Clause | 리포 동봉(별도 조항 없음) | **가능** | **채택 후보 1순위** |
| torchvision RAFT | flow 대조군 (D) | BSD-3 (torchvision) | BSD-3 | 가능 | 대조군 |
| OpenCV DIS / TV-L1 | flow 경량 대조군 (D) | Apache-2.0 | 해당 없음(비학습) | 가능 | 대조군 |
| **FlowSeek** (ICCV25) | flow 후보 (D) | Apache-2.0 | 리포 준용 | 조건부 | 주의: 의존 Depth Anything V2 **Base/Large 가중치는 CC-BY-NC** (Small만 Apache). Small 구성만 검토 |
| **SAM 2.1** (Meta) | mask 시간축 안정화 A/B (C) | Apache-2.0 | Apache-2.0 | **가능** | A/B 후보 허용 |
| **MiniMax-Remover** (현 v32 사용 모델) | 생성 인페인터(현행) | **리포 LICENSE 파일 없음** (기본 all-rights-reserved) | **CC-BY-NC-4.0 (HF 공식)** | **불가** | ⚠️ **production 채택 금지 — 아래 별도 절** |
| Wan2.1-1.3B (MiniMax의 base) | 자체 refiner base (I) | Apache-2.0 | Apache-2.0 | 가능 | Phase I 자체학습 base 후보 |
| **FloED** (HKUST/DAMO) | residual 생성모델 후보 (G) | Apache-2.0 | 리포 준용(명시 없음) + SD1.5/AnimateDiff 계열 base(OpenRAIL-M 사용제한 조항) | 조건부 가능 | ROI 벤치 후보 — 가중치 조항 추가 확인 후 |
| **DiffuEraser** (Alibaba) | residual 생성모델 후보 (G) | Apache-2.0 | **ProPainter prior 의존 → 그 부분 비상업 오염** | prior 교체 시만 | 명세대로: ProPainter prior 그대로면 불채택 |
| ProPainter | (DiffuEraser prior) | **NTU S-Lab 1.0 (비상업)** | 비상업 | **불가** | production 금지 |
| **VideoPainter** (TencentARC) | residual 후보 (G) | 리포에 라이선스 명시 미확인 | base=CogVideoX-5b-I2V: **상업 등록 필수 + 월 100만 방문 상한** | 등록 시 조건부 | 명세대로: 등록·조건 확인 전 금지 |
| **EraserDiT** | residual 후보 (G) | **공개 코드 미발견** (arXiv 2506.12853만) | — | — | 설계원칙만 참고 |
| **Object-WIPER** (CVPR26) | associated-effect 제거 설계 (C) | 리포 존재하나 **LICENSE 파일 미확인**(기본 all-rights-reserved) | training-free(대형 비디오 diffusion 활용) | 불명 → 코드 재사용 불가 | 설계원칙만 참고 (associated-effect mask 발상) |
| **EffectLearner / GenEraser** | Locator·Preserver 분리 설계 (C) | 공개 코드 미발견 | — | — | 설계원칙만 (GenEraser arXiv 2605.30045) |
| SEDiT (참고 발견) | mask-free 자막 제거 연구 | 미공개 (arXiv 2605.14894) | — | — | 설계원칙만 |
| OpenCV / NumPy / ffmpeg | 실화소 전파·합성 (E,F) | Apache-2.0 / BSD / LGPL(ffmpeg 동적링크) | — | 가능 | 채택 (현행 유지) |

## ⚠️ 중대 발견: 현행 MiniMax-Remover 가중치는 비상업(CC-BY-NC-4.0)

- 근거: HF 공식 모델카드 `zibojia/minimax-remover` license 필드 = cc-by-nc-4.0.
- 의미: **현재 v32 staging이 쓰는 생성 인페인터를 그대로 production(C0)에 가져갈
  수 없다.** 지금까지는 staging 실험이라 위반 아님. 그러나 RC4의 목표가 상용
  전환이므로 이 문제는 반드시 해소돼야 한다.
- 해소 경로 (RC4 설계와 정합):
  1. **실화소 우선 아키텍처(Phase E)로 생성모델 의존 축소** — residual hole만
     생성모델이 메우므로 교체 비용이 작아진다.
  2. residual 생성모델을 **상업 가능 후보로 교체**: FloED(조항 확인 후) 또는
     **Wan2.1(Apache) base + 합성데이터 자체 fine-tune(Phase I)** — 고객영상
     무학습 원칙과도 부합.
  3. (병행 가능) MiniMax-Remover 저자에게 상업 라이선스 문의 — 대표 결정 사안.
- RC4 동안 staging에서 MiniMax를 계속 쓰는 것은 허용(비교 기준선 역할).
  단 **최종 산출 파이프라인의 기본 경로는 비상업 컴포넌트 0**이 목표.

## 후속 실사 항목 (v2에서 갱신)

- FloED 가중치(구글드라이브 배포본)의 명시 조항 + SD1.5 OpenRAIL-M 사용제한 검토
- Wan2.1-T2V-1.3B: **Apache-2.0 확인 완료** (HF 모델카드 명시 — Phase I base 적격)

## 출처

- SEA-RAFT: github.com/princeton-vl/SEA-RAFT (LICENSE = BSD-3)
- SAM2: github.com/facebookresearch/sam2 (README: checkpoints/code Apache-2.0)
- ProPainter: github.com/sczhou/ProPainter (README: NTU S-Lab 1.0, 비상업 명시)
- DiffuEraser: github.com/lixiaowen-xw/DiffuEraser (Apache-2.0 + ProPainter 준수 요구)
- VideoPainter: github.com/TencentARC/VideoPainter · CogVideoX MODEL_LICENSE
  (등록 필수, 월 100만 방문 상한): github.com/zai-org/CogVideo/MODEL_LICENSE
- FloED: github.com/NevSNev/FloED-main (LICENSE = Apache-2.0, 2025.04 가중치 공개)
- MiniMax-Remover: huggingface.co/zibojia/minimax-remover (license: cc-by-nc-4.0)
- FlowSeek: github.com/mattpoggi/flowseek (Apache-2.0, NOTICE에 의존성 명시)
- EraserDiT: arxiv.org/abs/2506.12853 (코드 미발견)
- Object-WIPER: sakshamsingh1.github.io/object_wiper_webpage (CVPR 2026)
- GenEraser: arxiv.org/abs/2605.30045 · SEDiT: arxiv.org/abs/2605.14894
