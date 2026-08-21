# RC4 모델 SBOM (Software/Model Bill of Materials, Phase G2 — 2026-08-21)

모든 후보 컴포넌트의 **출처·버전(commit/revision)·가중치 SHA256·라이선스**를
고정 기록한다. 실측 방법: GitHub commits.atom(tip commit)·HF API `?blobs=true`
(revision + LFS sha256). 판정 근거는 `RC4_COMMERCIAL_MODEL_LICENSE_MATRIX_V2.md`.

## 1. 생성 엔진 후보

### MiniMax-Remover (연구 기준선 전용 — RED)
| 항목 | 값 |
|---|---|
| code | github.com/zibojia/MiniMax-Remover @ `28e12b450d8a72a7547b86940a4985e6ad90d75b` (LICENSE 없음) |
| weights | hf.co/zibojia/minimax-remover @ `889e41651d903bcef2d2aea307155812b6d326fd` (cc-by-nc-4.0) |
| transformer | sha256 `a379d98432970f614befb260357153edcd01a99748cf7f6dabe1a230c159b213` (2,254,157,576 B) |
| vae | sha256 `d6e524b3fffede1787a74e81b30976dce5400c4439ba64222168e607ed19e793` (507,591,892 B) |

### SVOR (후보 E/F — GREEN)
| 항목 | 값 |
|---|---|
| code | github.com/xiaomi-research/SVOR @ `df1fe23248c46477aea665c0f116fff91184f26d` (Apache-2.0) |
| LoRA repo | hf.co/HigherHu/SVOR @ `a2b23c835a6c046247ea1ed2aa83d075853e5ac4` (apache-2.0) |
| Stage1 LoRA | remove_model_stage1.safetensors `7846f8a188aa88904f55bcf6c49f0cbb9aaca2da4669dca50af75990ac6beb15` (544,330,008 B) |
| Stage2 LoRA | remove_model_stage2.safetensors `fd52a47c4c49f5f2d73e2b823c32ab245030060ead4c4ce3aa4d7198fc197b9d` (544,330,008 B) |
| base | Wan-AI/Wan2.1-VACE-1.3B (아래) |
| 학습 데이터 | Stage1: 비페어 실배경 영상(online random mask, self-supervised) · Stage2: 합성 페어 + RORD-50 |
| 논문 | arXiv:2603.09283 (Curriculum Two-Stage Training; MUSE mask 전처리) |

### Wan2.1-VACE-1.3B (후보 C/D + SVOR base — GREEN)
| 항목 | 값 |
|---|---|
| repo | hf.co/Wan-AI/Wan2.1-VACE-1.3B @ `574e6a744642ce3bee319afc31496b88bde8aac4` (apache-2.0) |
| DiT | diffusion_pytorch_model.safetensors `c46a6f5f7d32c453c3983bbc59761ea41cd02ad584fb55d1a7ee2b76145847a2` (7,146,067,848 B) |
| VAE | Wan2.1_VAE.pth `38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981` (507,609,880 B) |
| T5 인코더 | models_t5_umt5-xxl-enc-bf16.pth `7cace0da2b446bbbbc57d031ab6cf163a3d59b366da94e5afe36745b746fd81d` (11,361,920,418 B) |
| VACE 코드 | github.com/ali-vilab/VACE @ `48eb44f1c4be87cc65a98bff985a26976841e9f3` (Apache-2.0; LTX 변형 RAIL-M — 미사용) |

### Wan2.1-VACE-14B (heavy 상한 후보 G/H — GREEN)
| 항목 | 값 |
|---|---|
| repo | hf.co/Wan-AI/Wan2.1-VACE-14B @ `539c162b1387eac9dc4c20bd3f74671309e76a4c` (apache-2.0) |
| DiT 샤드 1/7 | `569d54a07279b89f8281421fccf27ee2459ea853ce6845d3536b8664b0070078` (9,887,603,256 B) |
| DiT 샤드 2/7 | `b17ff172f262b4da91c31e8c46d3b7707f62cecfff6e18dd0072ab29eb350e46` (9,839,059,648 B) |
| DiT 샤드 3/7 | `741fd1508cd0288f0bb0f4fb3df6734da521d5ff23746170c848564a367e2cea` (9,839,059,744 B) |
| DiT 샤드 4/7 | `47b08ba289b127fc64979bc877ee07e1c87aeb8eb8cd47bcfd112682847981ba` (9,839,059,744 B) |
| DiT 샤드 5/7 | `29e35f4cf0e3a61f4726c960f1babf71d65677a8c99fe704257d665c9498ee88` (9,839,059,744 B) |
| DiT 샤드 6/7 | `91ac087a40b814331e87f5f015af11ba999275d48d2af49591b6bf7727d3c146` (7,910,235,256 B) |
| DiT 샤드 7/7 | `d2c107015cdef963cb8f9b7006330e7d7355ca648d90af32bcf80ed76d8eba1f` (6,098,227,760 B) |
| VAE·T5 | 1.3B 리포와 동일 파일·동일 sha256 (위 참조) |

## 2. flow·mask·평가 컴포넌트

| 컴포넌트 | 출처 @ 버전 | 라이선스 | 역할 |
|---|---|---|---|
| SEA-RAFT | github.com/princeton-vl/SEA-RAFT @ `9137517ba24e628442aec097d3afe71d03503b75` | BSD-3 (가중치 dataset 조건 YELLOW) | GPU flow 후보 (G5) |
| torchvision RAFT | torchvision 내장 (버전은 Modal 이미지 pin 시점에 기록) | BSD-3 (동상 YELLOW) | GPU flow 대조군 (G5) |
| SAM 2.1 | github.com/facebookresearch/sam2 @ `2b90b9f5ceec907a1c18123530e92e794ad901a4` | Apache-2.0 (code+weights) | mask 시간축 안정화 A/B |
| FGT | github.com/hitachinsk/FGT @ `b6b01e3fc82931e050cf4d7062f3879f70677bad` | MIT (가중치 조항 없음 — YELLOW) | 필요 시 구조형 국소 비교 (G8) |
| ISVI | github.com/hitachinsk/ISVI @ `ec395183a906955e2c8b2ee300d92799b71b9572` | MIT (동상 YELLOW) | 필요 시 구조형 국소 비교 (G8) |
| PROVE | github.com/xiaomi-research/prove @ `7ca299a7a5e12f0fb8285fb58ae744f691607b35` | Apache-2.0 (+DINOv2-giant Apache) | RC-S/RC-T 오프라인 평가 |
| OpenCV/NumPy/ffmpeg | 현행 v32 이미지 pin | Apache/BSD/LGPL(동적링크) | 실화소 전파·합성·인코딩 |

## 3. 갱신 규칙

- 실제 다운로드하여 Modal volume/이미지에 넣는 시점에 **다운로드본 sha256을
  이 문서의 값과 대조**하고, 불일치 시 즉시 중단·보고한다 (공급망 검증).
- 새 컴포넌트 추가·버전 변경 시 이 문서에 행 추가(기존 행 수정 금지, 이력 보존).
- MiniMax 행은 서면 허가 결과에 따라 GREEN 전환 또는 Phase N에서 제거 처리.
