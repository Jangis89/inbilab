# V32 출력 검증 계층 구조 (Phase D — 2026-08-18)

"손상된 결과물이 고객에게 나가는 것"을 막는 다층 방어. 모든 계층은
`gpu-worker/handler_v32.py`에 내장되어 finish 단계에서 실행된다.

## 계층 설계

| 계층 | 무엇을 | 언제 | 비용 | 검출력 (fault-injection 실측) |
|---|---|---|---|---|
| L0 세그 단위 | 각 세그먼트 업로드 전 크기>0, ffprobe 성공 | 세그 완료 시 | ~0.1s/세그 | 세그 자체 손상 |
| L1 표준 검증 `validate_output_layer1` | ffprobe(코덱·해상도·FPS·duration) + packet count(frame_count_fast, 해독 없음) + audio duration + **샘플 디코드 4구간**(시작·중간·끝·세그 경계, `-v warning` + 손상 regex) | **모든 finish에서 항상** | 4.7s (177초 영상) | **11/12** (F01~F11), 오탐 0 |
| L2 전수 디코드 `deep_audit_full_decode` | 전체 프레임 병렬 8-way 디코드 + 손상 regex | `deep_audit` 플래그 시 (골든런·canary C0는 100%) | ~7s (20초 클립), 177초 영상 ~40s | **12/12** (F12 중간 512B 손상 포함) |
| L3 품질 게이트 | control re-encode 대비 PSNR/SSIM/flicker + 증거 크롭 육안 | 골든런에서만 | 분 단위 | 제거 품질 회귀 |

## fault-injection 결과 요약 (원자료: VALIDATOR_FAULT_INJECTION_V32.csv)

- F01 truncate / F02 중간손상 / F03 세그누락 / F04 중복 / F05 순서 / F06 TS 불연속 /
  F07 비디오없음 / F08 오디오없음 / F09 오디오길이 / F10 FPS / F11 끝 GOP: **L1이 검출**
- F12 (중간 512B 미세 손상): L1 샘플 디코드가 우연히 비켜감 → **L2 전수 디코드가 검출**
- REF(정상 파일): L1·L2 모두 통과 (오탐 없음)

## 실패 시 동작

L1 실패 → finish가 예외를 던져 작업 **FAIL** 처리 (서명 URL 미발급, 고객 미노출).
큐 서비스는 상태를 failed로 기록하고 에러 문자열 보존. 부분 산출물은 wmtmp-v32에
남아 사후 분석 가능.

## 운영 정책 (RC)

- 일반 트래픽: L0+L1 항상 (총 ~5초, finish tail 27.5s에 포함됨)
- C0 내부 canary: `deep_audit=True` 100% (L2 상시)
- 정식 운영 후: L2는 표본률로 낮추는 안을 canary 데이터로 결정 (GO/NO-GO 이후)
