# RC4 상업 모델 라이선스 매트릭스 v2 (Phase G2, 2026-08-21)

- 원칙(새 명세 §G2): production 후보의 모든 라이선스가 GREEN이어야 하며,
  RED 모델은 production manifest에서 제외한다. staging 실험은 허용.
- v1(`RC4_SOTA_RESEARCH_AND_LICENSE_MATRIX.md`)을 대체하지 않고 확장한다 —
  v1의 FloED/DiffuEraser/ProPainter/VideoPainter 등 판정은 유지.
- 실사 방법: 공식 GitHub repo·HF 모델카드를 2026-08-21에 직접 열람(WebFetch),
  HF API로 license 필드·revision·가중치 LFS SHA256 실측. 커밋/리비전은
  `RC4_MODEL_SBOM.md`에 고정.

## 판정 요약 (필수 12종)

| # | 후보 | 코드 | 가중치 | base | 상업 SaaS | 판정 |
|---|---|---|---|---|---|---|
| 1 | MiniMax-Remover | 리포 LICENSE 없음 | **CC-BY-NC-4.0** | Wan2.1-1.3B(Apache) | **불가 (서면 허가 전)** | **RED** — 연구 기준선(후보 B) 전용 |
| 2 | Wan2.1-VACE-1.3B | Apache-2.0 (ali-vilab/VACE) | Apache-2.0 (HF 모델카드 명시) | Wan2.1-T2V-1.3B(Apache) | 가능 | **GREEN** |
| 3 | Wan2.1-VACE-14B | Apache-2.0 | Apache-2.0 (HF 모델카드 명시) | Wan2.1-T2V-14B(Apache) | 가능 | **GREEN** |
| 4 | SVOR code (xiaomi-research/SVOR) | **Apache-2.0** | — | — | 가능 | **GREEN** |
| 5 | SVOR Stage1 LoRA (HigherHu/SVOR) | — | **Apache-2.0** (HF license 필드) | Wan2.1-VACE-1.3B(Apache) | 가능 | **GREEN** (dataset 각주 1) |
| 6 | SVOR Stage2 LoRA (HigherHu/SVOR) | — | **Apache-2.0** (HF license 필드) | Wan2.1-VACE-1.3B(Apache) | 가능 | **GREEN** (dataset 각주 1) |
| 7 | SEA-RAFT | BSD-3-Clause | 리포 배포(별도 조항 없음) | — | 코드 가능 · 가중치 각주 2 | **YELLOW** (학습 dataset 조건) |
| 8 | torchvision RAFT | BSD-3 (torchvision) | BSD-3 (torchvision 배포) | — | 코드 가능 · 가중치 각주 2 | **YELLOW** (학습 dataset 조건) |
| 9 | SAM 2.1 (Meta) | Apache-2.0 | Apache-2.0 | — | 가능 | **GREEN** |
| 10 | FGT (hitachinsk, ECCV22) | MIT | 별도 조항 없음(Google Drive/HF 배포) | — | 코드 가능 · 가중치 불명 | **YELLOW** (가중치 조항·YouTube-VOS 계열 학습 데이터) |
| 11 | ISVI (hitachinsk, CVPR22) | MIT | 별도 조항 없음 | — | 코드 가능 · 가중치 불명 | **YELLOW** (동상) |
| 12 | PROVE (xiaomi-research) | Apache-2.0 | 의존 DINOv2-giant = Apache-2.0 | — | 가능 (오프라인 평가 전용, 제품 미탑재) | **GREEN** |

보조 행: VACE 리포(ali-vilab/VACE) 코드 Apache-2.0 — 단 **VACE-LTX-Video-0.9
변형은 RAIL-M**이므로 사용하지 않는다 (Wan2.1 변형만 사용).

## G2.1 세부 필드 (필수 12종)

### 1. MiniMax-Remover — RED
- repository license: 없음(기본 all-rights-reserved) · model card/weights: cc-by-nc-4.0
- redistribution/SaaS/attribution: NC 조항으로 상업 배포 불가. **G1 서면 허가 문의
  발송(2026-08-21) — 허가 확보 전 production 코드·이미지·volume·manifest 포함 금지.**
- embedded prior: Wan2.1-1.3B(Apache) — base는 문제 없으나 학습된 remover 가중치가 NC.

### 2·3. Wan2.1-VACE-1.3B / 14B — GREEN
- 모델카드 원문: "The models in this repository are licensed under the Apache 2.0
  License." + 생성물 권리 불주장(가공 출력 상업 제공 가능).
- 동봉 구성요소: Wan2.1 VAE·umT5-xxl 인코더(구글 umT5 체크포인트, Apache 계열)·
  diffusion transformer — 전부 동일 리포에서 Apache로 배포.
- 사용 제한: 위법·유해 콘텐츠 금지 조항(일반적 AUP) — 당사 용도(자막 제거)와 무관.
- attribution: Apache-2.0 NOTICE 보존 요구 → RELEASE_MANIFEST에 명시 예정.

### 4~6. SVOR — GREEN
- 코드: xiaomi-research/SVOR, LICENSE = Apache-2.0.
- LoRA 2종(remove_model_stage1/stage2.safetensors): HF HigherHu/SVOR license
  필드 = apache-2.0. base = Wan2.1-VACE-1.3B(Apache) → 전체 체인 상업 명확.
- **각주 1 (dataset condition)**: Stage1은 비페어 실배경 영상 self-supervised,
  Stage2는 합성 페어 + RORD-50. 학습 데이터 자체의 조항은 저자 책임 영역이며
  가중치의 명시 라이선스(Apache)가 배포 조건. 저자가 상업금지 데이터로 학습한
  정황은 발견되지 않음. 위험도 낮음으로 기록하되 SBOM에 사실 그대로 남김.

### 7·8. SEA-RAFT / torchvision RAFT — YELLOW
- 코드는 각각 BSD-3-Clause로 상업 명확.
- **각주 2 (dataset condition)**: 공개 flow 가중치는 FlyingChairs/FlyingThings3D
  (Freiburg, 연구목적 고지)·Sintel·KITTI(CC-BY-NC-SA) 등으로 학습된 것이 관행.
  torchvision은 가중치를 BSD-3로 배포하지만 학습 데이터 계열은 동일.
- 대응: ① 개발·벤치(G5)에는 그대로 사용(연구/개발 단계) ② production 채택 확정
  시점(Phase M/N)에 최종 가중치의 학습 데이터 조항을 개별 재확인하고, 불명확하면
  (a) OpenCV DIS/TV-L1(비학습, Apache) 폴백 또는 (b) 허용 데이터로 자체 재학습을
  대표에게 선택지로 보고. **flow 후보 확정 전에는 이 항목이 게이트를 막지 않는다.**

### 9. SAM 2.1 — GREEN
- 코드·체크포인트 모두 Apache-2.0 (공식 리포 명시). mask 시간축 안정화 A/B 후보.

### 10·11. FGT / ISVI — YELLOW
- 코드 MIT. 사전학습 가중치에 별도 조항 없음(=코드 라이선스 준용이 통상 해석이나
  명시 부재) + 학습 데이터(YouTube-VOS 계열, 연구목적 고지) 조건 불명.
- 새 명세 §G8 위치 그대로: **"필요 시 구조형 비교"의 국소 실험만** — flow 계열
  구조 붕괴 사례가 실제로 나올 때만 staging 비교에 투입. production 채택하려면
  저자 확인 필요. 현 시점 production 후보 아님.

### 12. PROVE — GREEN
- Apache-2.0. RC-S(공간 응집)·RC-T(시간 응집) 평가 프레임워크.
- 의존: DINOv2-giant(Apache-2.0), PyTorch 2.6+, Transformers 4.51+.
- 오프라인 품질 평가 전용 — 고객 파이프라인에 탑재되지 않음.

## production manifest 규칙 (G2.3)

```text
RED  → production manifest 제외 (현재: MiniMax-Remover weights, ProPainter,
       VACE-LTX-Video, CogVideoX 계열 [v1 판정 승계])
YELLOW → staging/벤치 사용 가능. production 채택 확정 전 개별 재실사 필수
       (현재: SEA-RAFT·torchvision RAFT 가중치, FGT·ISVI 가중치)
GREEN → 제한 없음 (Wan2.1-VACE 1.3B/14B, SVOR code+LoRA, SAM2.1, PROVE,
       OpenCV/NumPy/ffmpeg [v1 승계])
```
