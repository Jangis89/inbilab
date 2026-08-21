# RC4 상업 엔진 단계 시작 상태 (Phase G0 동결 기록)

- 날짜: 2026-08-21
- 유효 명세: `CLAUDE_V32_RC4_COMMERCIAL_ENGINE_SVOR_VACE_FINAL_SPEC.md`
  (이전 RC4 REV2 명세의 Phase A~F 산출은 기준선으로 보존, G0부터 이어서 진행)
- 목적: SVOR/VACE 상업 엔진 토너먼트 시작 시점의 코드·기준물·설정을 고정 기록

## G0.1 기준선

| 항목 | 값 | 확인 방법 |
|---|---|---|
| base 브랜치 | `perf/wm-v32-rc4-sota-hybrid` (RC4-18) | branches 페이지 |
| 새 작업 브랜치 | `perf/wm-v32-rc4-commercial-engine` — base에서 분기 (2026-08-21) | 생성 확인 URL "branch was created" + tip 실측 |
| 새 브랜치 tip | `f0c4dbf7fd3e438e0ba69b0e0ff38233be8f2389` | tree API 응답 currentOid 실측 |
| PR #1 | Open 유지 — merge/close/force-push 금지 (대표 승인 전) | branches 페이지 |
| 운영 | v29 유지 · V32 일반 트래픽 0% · permanent GPU 0 · production S3 key 미발급 | 승계 |
| Modal staging | `inbilab-wm-gpu-v32-speed-staging` (현행) · SVOR용 별도 앱 `inbilab-wm-svor-staging` 예정(G6) | — |

## G0.2 동결 — 코드 SHA256 (새 브랜치 raw에서 실측, 2026-08-21)

| 파일 | SHA256 |
|---|---|
| gpu-worker/handler_v32.py (flow 라우팅·mask·Preserver·un-blend·카드 검출 = 현 flow/mask 구현) | `7f74b7023f364fb6ffbb9a444adfc857cfad471855018a53e2765b45b6003690` |
| gpu-worker/restore_rc4.py (실화소 전파 엔진) | `f911c22383f61d49ba247c35b59918e2dab845e02dde41b0f3418ef7caa25ab2` |
| gpu-worker/scripts/golden_run_v32.py | `bcef1ab084e54fda987cd62f613913ba97ea814fb2e0a4f72118b2809bca86bd` |
| gpu-worker/scripts/preserve_assets_v32.py | `efc8e8a0502cad3af0c6590f0a950e227c06c4ebe6cfa7c7eb03be6c22ff0642` |
| gpu-worker/scripts/make_holdout_v32.py | `680328b211bf64281e72163b3baac4465e034b2946a76ab2faefc01ec1dce3af` |
| gpu-worker/scripts/uat_run_v32.py | `d6db78fc0737d7a319161751e21b2a34dd26e381e64ef2e142106b5485b0b5a0` |

### RC4-18 런타임 설정 (handler_v32.py 기본값)

```text
WM_RC4_FLOW=1  WM_RC4_EFFECT=1  WM_RC4_PRESERVE=1  WM_RC4_VARBLEND=1  WM_RC4_CARD_SCAN=1
WM_SEG_NPROC=6  WM_S3_CONC=8  WM_S3_PART_MB=16  WM_BACKEND_NAME=modal-v32
```

## G0.2 동결 — 기준물 SHA256

### UAT source 3편 (승계 — `V32_RC4_START_STATE.md`에서 동결, INC-3 종결 시 3/3 재검증 일치)

| UAT | 프로젝트 ID | source SHA256 | bytes |
|---|---|---|---|
| UAT-01 | bad96c77-b609-4eaf-9de6-cfddc07e4c18 | `32958d0e918f5bed859ed040d5d6ccd3db959ace0e9cde5c8300fc3e68a71be6` | 159,459,560 |
| UAT-02 | d5a9ae7f-3960-4914-b048-b3953a3d5245 | `974e4cd9bec29c708de3e7478a73764291637d9705eedaa2e2fda621b5e5da59` | 159,536,565 |
| UAT-03 | 5ddc260c-8519-4ec1-9f41-6fdef71031b8 | `9be1239a7358a6671b4988aad5d9ac8c138cd428782a6a87c84023b3ea36ae7e` | 119,951,400 |

### RC2/RC3 출력 (승계 — `RC4_BASELINE_RC3_OUTPUTS_SHA256.md`, bench-assets/uat-preserve/ 보존본)

| 파일 | SHA256 |
|---|---|
| rc2_o1 | `960ba22aaed87be63bfaddd81032d1475a11fb864044f8f4697fe9645343faf5` |
| rc2_o2 | `5b3b3b63bbc4641c3c8e7800f3273616a53b67b0e4eed32459d9b2604d919358` |
| rc2_o3 | `44f44da117d9e5634a013bd2700bf886a1298278653962cb44bba6c1eba0b5ad` |
| rc3_o1 | `8398865a669aaed90b81d426256a2baff4c1551071c11e0e60ae0b497083a975` |
| rc3_o2 | `44ea49e76c6d66d393f135db9af5957c7d02cdbe53e4a311ecd7a468a6f32fb9` |
| rc3_o3 | `09ab23b54584e8864126cf9cb5a03f3ae3bb32a6a9f471b6c072d1b40dec4c2c` |

### blind holdout 20종

- manifest: `docs/RC4_BLIND_HOLDOUT_MANIFEST.csv` + `docs/RC4_BLIND_HOLDOUT_FREEZE.md` (승계, 변경 없음)
- 동결 규칙 유지: 개발 중 실행 금지, Phase L에서 첫 채점. 채점 후 threshold 재조정 시 세트 폐기·재제작.

### golden 35종 (입력/GT — videos-clips/bench-assets/golden/, run #191 preserve 액션으로 전수 재해시)

- 재해시: workflow run #191 (`preserve` 액션, ref=이 브랜치, 2026-08-21) — 존재 객체 60/60 성공.
- g06~g10(실영상, GT 없음)·g21~g25(negative — 실물 텍스트/사각형, 제거 0 요구, GT 없음)는 설계상
  `_clean.mp4`가 존재하지 않음 → run 로그의 `fail=10/70`은 이 구조를 재확인한 것 (데이터 손실 아님).

| 파일 | SHA256 | bytes |
|---|---|---|
| g01_input | `68863b96f15731687cd824cfb6c59e22a4be81693e1e6bacefcf51ee6c19d1e0` | 32,802,241 |
| g01_clean | `18fcb063cfcde57b78603816c39423602b9e5070fadc1e1a8081073ed79b0438` | 33,857,690 |
| g02_input | `9ba27d1bf06dc2a323119d565ebd62452eb0d9688e73e3d0f5bab000de76b84b` | 27,590,283 |
| g02_clean | `365c370fd74db0287eb888e4ada4a3ac710098059e4bfc2594af0fde1c654b30` | 28,875,073 |
| g03_input | `2574b110aa0383ebcab18bd533206b2013952aeee4db4e882c793ab37bb96107` | 23,580,115 |
| g03_clean | `d0cb5293ecb75ec2638f0b3778d6c18215ae2fb356818a7c2b9cc90eed0867c1` | 24,917,260 |
| g04_input | `75e8a0348ff7bc233059da5ed9b1c252a888f81703bdd6ce6675bf8d70d90324` | 21,720,446 |
| g04_clean | `7a0ebfe50fb5b2fd07fd8d0a2ebec8d06d3aeb531e9da1b1d179b5b21bc369ea` | 20,530,683 |
| g05_input | `325e4590c61b210631d287e2ac6debeada95f672b02166f72b45ee46fd1aaa3f` | 24,925,158 |
| g05_clean | `78248fbac7684a8d8e6750f8b693055ecfbb70c7e20c735e8ec3b0bac64c04a6` | 25,711,046 |
| g06_input | `01cadf29d3fafd0a6d0d63cd344012dadd63baa23fdf8e2781c35017a47172c5` | 48,599,032 |
| g07_input | `6b2cbb4a8dcc9494e7b3e3fb99536391f9e8d0f0dc985c6b8e7beb3962fde8c3` | 50,463,939 |
| g08_input | `13fc47d903da6e01fa1aa658eccac042c36e20a0d2988f27fb77a16ed5aaaa23` | 39,411,392 |
| g09_input | `e8920d5ddde7c5ac2b69878903c948cb03976b06519ae64dcfd56cd8b8a6aa61` | 31,379,046 |
| g10_input | `cb61c038eeb14f54ff3302717641b009df916393731289a28173fecb7b82f2a2` | 39,390,466 |
| g11_input | `404078f046c0fd89ab5a08cb7aa4fc89d7b925828c473bb36f1997aed9a845d7` | 31,236,880 |
| g11_clean | `e8a19a82bb081521f367405ac8a359f282e50ac1d399ab5b0efc286a7d7788d6` | 33,341,558 |
| g12_input | `3938e18108714a2456ecd17297120b13ee4334c4d2ac803fd3beff056044ade0` | 30,854,366 |
| g12_clean | `ec432b652d379f3b88b350d3e23d92e584df80f1099ae63c55e4f973b8d7e697` | 31,094,112 |
| g13_input | `7cde15788e4b002ffffbde1a2a4e80aaa4abff37f898548110449c62c0d4986b` | 26,529,915 |
| g13_clean | `46a02562b54686c2e5e43ece3a3175cbe6402cf57c8dd4935acfcf7366538bce` | 28,372,246 |
| g14_input | `4de66ce2fb2965b84991ebb325ad90c1fd9876dc279df04c02d50ff7e56cd7c5` | 24,353,182 |
| g14_clean | `ebc89adb2643caaba436f273e069c94ddc07838b7a7e5e896f3190aedacf1683` | 25,110,341 |
| g15_input | `802583fd89c97f9f496cc93a49bcf2c839eb6de55aa5a57baec3421a7c5e37ed` | 25,726,508 |
| g15_clean | `cf3cacbf5b55e9c796a3f4efc9dc9131837f46db097e716c3cdc0fa320d8de7d` | 27,186,618 |
| g16_input | `67296ef208dc6250366090f74c203e7a25640a9a1683a7d54205ccc9a52eb627` | 37,948,856 |
| g16_clean | `dfdf7628e9cdcbdf45e8a20ef0b4ee8a7c35b8544221cc52b006b42bacc2c817` | 39,348,782 |
| g17_input | `6b1b237b28a04586fa3b959626ba60b8fe4e2cdcbaee053f83720415e7de2051` | 29,647,235 |
| g17_clean | `2e8b191daa73462e1ac4c89e928e92a109ab80317c7a0f6e3b2ce975c7c13a98` | 31,194,790 |
| g18_input | `adb303c364f565e969c2ceae3573ed5a1cf23e5b6da86595a32281105fe10b0a` | 28,114,345 |
| g18_clean | `63a8fee1763b2d7e8802794a449d0e13c84694fd90bd1aa997ba96d43c874288` | 28,372,231 |
| g19_input | `617bb744bf2ece4561746f6c627eb2c4193a185484e5242e559e4bc118c13869` | 26,790,886 |
| g19_clean | `be83da405de17f1dccfdbff23a67db98ad647623ecd7f8382ea697bb3e2e634f` | 27,737,941 |
| g20_input | `80b8d5b86d504cdf6e6a368a389cd1145f5a6f325750e0435fd7a59bc1a1f7f2` | 26,420,841 |
| g20_clean | `4ecf3e818278af67a39950e1244eb3bf6969c725b15e65223986099d72fe1454` | 27,701,121 |
| g21_input | `f2fc1ac138a1abc4d3e2e4ec70634a8a6c48522591364560d55d3a005607a39f` | 1,656,181 |
| g22_input | `65b8747ffd2a5a33788838dd89843f53eecacfe4422beab8454f94b589f27e11` | 1,116,693 |
| g23_input | `1477e7856f4b0702196fc556eb0406669b52516fd2ef03bf71f146365b4cc90e` | 633,941 |
| g24_input | `d837333c1dbd3df47753759b51ea5357ebc91dce0940c0cae838ac45c3663a44` | 1,443,209 |
| g25_input | `a90105b3197ee0461afc683e5456d922bbd8e708b8b489f0bfc9aba66919a012` | 987,230 |
| g26_input | `b215545ef66dcf3f6754b7bb0d977383123dc8a49a4d619287c70c23da2849f5` | 21,892,651 |
| g26_clean | `2a929a4289c8fd98f537484de3fbfc621eb405dd4263aa9e7921254743594a92` | 22,098,559 |
| g27_input | `4e500d3290e65ee43fe3a8753bf93c50f50ad49cb26914ebc1519118a7ffd57b` | 26,637,043 |
| g27_clean | `31ba13b54c923b9f582281547b7758b868580903c04feded3bec0a7792c5f1a5` | 29,176,667 |
| g28_input | `f72e996726b92223dd8324af5bd56095b463442fe7cfe0df81b8755e132a064b` | 56,629,316 |
| g28_clean | `e0016c6af5f27887a6188665ccad0bc65164fcf3f81cb469b993e1b732191293` | 58,869,849 |
| g29_input | `84460a5d0145195b63316347d3bb0f41dab356430849528bfaf6f87e386206a7` | 26,436,104 |
| g29_clean | `4042719a7155bf96086c6f862965f0c40604a51a5b12cdedbe2cf178540d4e43` | 26,485,409 |
| g30_input | `5c5b3dd1b6392164052ee620acb8eb44799e97e8fad6590e2bae847b8a0108b8` | 70,989,052 |
| g30_clean | `4e3acf5841959e2c2dae9c8b2cce81b15d5b1128c37472123867d51c7746f360` | 82,549,767 |
| g31_input | `839db59e1abb0497351ad574efeb7856f37cf0aa23d333a192fa8cda5396985f` | 66,071,117 |
| g31_clean | `5bf265f54014685a5710329666a6659bf42959f4eb00b537d4b0bb57db0a1c83` | 71,153,451 |
| g32_input | `dcac5c1d69347e3e970682a3ce23b9473a8d07a5900a5fb08bf2551eee7930b8` | 36,359,986 |
| g32_clean | `f268164363c8b1e73269b52580c99d150ebe81930305138ce0a39018a89bd7c3` | 36,780,969 |
| g33_input | `1b3c887c6efcbdd63bd052cd350c6a683317b11ad179fe8af9e7ae5c76f96425` | 27,600,820 |
| g33_clean | `bb7a7047d65f7fa193bbbb181bc2afbc5fd08d28bd56ee112a3d3124703b3fb2` | 28,993,290 |
| g34_input | `56aef8b1bd69909d8efe717a8c4495491e698f7f3067bf87bf0e535868dd78e3` | 1,542,137 |
| g34_clean | `f525aa11dbccc907a6a8d5f9c1948c3bfafbd80d60a667bc41c13ff8d4aba08a` | 1,373,941 |
| g35_input | `fcfaf489351eb889a396de538865804fe8964430ec384654003a0c9e299e3a61` | 23,493,373 |
| g35_clean | `05724ec31ee7b15028108447b288a00fab804cd4ec905fbb0b932ad572efbf3f` | 24,051,062 |

### 현 RC4 problem ROI (토너먼트 공통 비교 구간 — 동결)

| 소스 | ROI |
|---|---|
| golden | g26 (카드+질감, 19.05dB), g27 (20.17), g28 (34.64), g31 (25.15), g33 (26.95) — RC4-18 잔여 실패 5종 |
| UAT-01 | t100~115 (기와/격자 구조), t154 (아이 다리 보존) |
| UAT-02 | t5 (카드 밖 불투명화 0), t92~112 · t143~161 (카드 얼룩), t150 (붉은 오염) |
| UAT-03 | t45~60 (fast path 품질 유지 확인) |
| 외부 기준 | Vmake 결제 결과물 UAT-01·UAT-02 (대표 확보분) — 동일 ROI로 비교 |

### MiniMax-Remover 가중치 (연구 기준선 전용 — production 금지)

- HF repo: `zibojia/minimax-remover` · license 필드 **cc-by-nc-4.0** · revision `889e41651d903bcef2d2aea307155812b6d326fd` (HF API 실측 2026-08-21)

| 파일 | SHA256 (HF LFS 메타) | bytes |
|---|---|---|
| transformer/diffusion_pytorch_model.safetensors | `a379d98432970f614befb260357153edcd01a99748cf7f6dabe1a230c159b213` | 2,254,157,576 |
| vae/diffusion_pytorch_model.safetensors | `d6e524b3fffede1787a74e81b30976dce5400c4439ba64222168e607ed19e793` | 507,591,892 |

- 정직 고지: 위 해시는 HF 공식 저장소 메타데이터 실측값. Modal volume에 캐시된
  사본의 byte 단위 대조는 Phase N(격리/제거) 시점에 GPU 없이 CPU 잡으로 수행 예정.
- 서면 허가 확보 전 production 코드·이미지·volume·manifest에 포함 금지 (명세 §12).

## 롤백 지점

- 이 브랜치의 모든 실험은 `perf/wm-v32-rc4-sota-hybrid` tip(RC4-18)으로 즉시 롤백 가능.
- RC3 rollback 태그 `wm-v32-rc3-restoration-quality` = 1261b042 (승계).
