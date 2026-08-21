# RC4 상업 엔진 단계 진행 로그 (Phase G — 갱신형)

브랜치: `perf/wm-v32-rc4-commercial-engine` · 명세: COMMERCIAL_ENGINE_SVOR_VACE_FINAL_SPEC

## 2026-08-21 (1일차)

### 완료 — 전부 실측 검증
- **G0**: 브랜치 생성(RC4-18에서 분기, handler sha `7f74b702` 일치 실측).
  `RC4_COMMERCIAL_ENGINE_START_STATE.md` — 골든 60객체 전수 sha256(run #191),
  UAT 3편·RC2/RC3 출력·MiniMax HF 가중치·코드 sha·설정 동결.
- **G1**: MiniMax 상업 라이선스 문의 발송·보낸편지함 확인
  (`MINIMAX_COMMERCIAL_LICENSE_INQUIRY.md`, 2026-08-21 19:05 KST). 3영업일
  무응답 시(≈08-26) follow-up 초안 준비.
- **G2**: `RC4_COMMERCIAL_MODEL_LICENSE_MATRIX_V2.md` + `RC4_MODEL_SBOM.md`
  (필수 12종: GREEN=SVOR code/LoRA·VACE1.3B/14B·SAM2.1·PROVE,
  YELLOW=RAFT계열·FGT·ISVI 가중치, RED=MiniMax).
- **UAT 원본 안전조치**: bench-assets/uat-src/ 서버측 복사, sha 3/3 동결값 일치
  (run #195).
- **G6 SVOR 별도 앱** `inbilab-wm-svor-staging` (modal_app_svor.py +
  svor_worker.py + svor_run.py + svor* 워크플로 액션):
  - svordeploy #192 성공, svordownload #193 — Volume 적재 + **SBOM sha 5/5 일치**
  - **smoke #196 성공**: 81f 720×1280 BF16 20steps ·
    H100! run 239.1s / H200 230.6s · VRAM 29.2GB · load ~85s
    (flash-attn 미설치 → SDPA fallback. flash-attn on/off A/B는 후속)
- **G3 harness**:
  - handler_v32 **RC4-19**: segment_v32 `mask_export` 스위치 — 파이프라인이
    실제 손대는 allowed mask를 프레임 단위 npz로 동결 (플래그 없으면 무동작)
  - `tournament_extract_v32.py` (roiextract 액션): scan→ROI part만
    mask_export 실행→bench-assets/tournament/{roi}/{input,mask,cand_A}.mp4
    +meta(sha256) 업로드. g26 팩 완료(run 32475095215, 600f, miss 0).
  - `tournament_contact_v32.py` (tourncontact): 그리드 PNG + 나란히 mp4 →
    {UID}/rc4_review/tourn_*. g26 3시점 생성·육안 확인.
  - `tournament_metrics_v32.py` (tournmetrics): PSNR in/out·SSIM·sharp_ratio·
    flicker·out_maxdiff CSV.

### g26 첫 토너먼트 결과 (동일 입력·동일 동결 mask)
- 육안: cand_A(현 flow) 카드 잔상 잔존 / **cand_E(SVOR)·cand_C(순정 VACE)는
  카드 소거 + 가려진 앞치마 무늬 복원, GT 근사**.
- 지표(g26, C/E는 첫 81f 창): sharp_ratio A 0.866 vs **C 0.997 / E 0.977**,
  psnr_in A 15.53(600f) / C 16.63 / E 15.58, flicker 전원 <1.
  out_maxdiff C/E 106~112는 Preserver 합성 feather 링 화소의 국소값(2~3px) —
  합성 정책은 Phase H에서 확정.
- 실행비용: 81f 창당 ~240s(H100). 참고: cand_A/C/E 프레임 수가 달라
  (600 vs 81) 절대비교는 다음 라운드에서 동일 창으로 재산출 예정.

### 진행 중
- roiextract 잔여 10 ROI (g27/g28/g31/g33 + UAT 6구간) — run 32475669837

### 다음
1. 전 ROI 팩에 후보 C/E 실행(svorroi) + A 동일창 재지표
2. 후보 D/F(flow real-pixel → residual hole만 SVOR/VACE) 배선
3. G4 MUSE A/B (SVOR 파이프라인 내장 MUSE + 자체 mask 전처리 A/B)
4. G5 GPU flow(RAFT/SEA-RAFT H100!/H200) 벤치
5. G7 VACE-14B smoke (H200→B200)
6. PROVE RC-S/RC-T 통합 (svor 이미지에 op 추가)
- Vmake 결과 2편(UAT-01/02)은 대표 첨부 대기 — 도착 시 같은 창으로 비교표 포함
