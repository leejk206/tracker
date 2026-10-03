# tracker — 크로스체인 자금 흐름 추적

BTC tx·주소, HL 계정, 또는 **아무 지원 체인의 주소** 하나를 넣으면 체인을 넘나드는 자금 흐름을 따라가고,
구간마다 연결 확실도를 붙여 보고서로 뽑는다. 거래소·브리지·믹서에 닿거나 상한에 걸리면 멈추고 다음 추적 리드로 남긴다.

- 체인: BTC(→HL) · Hyperliquid · Ethereum · Arbitrum · Base · Optimism · Polygon · Tron
- 체인 사이: HyperUnit(BTC/ETH/SOL ↔ HL), HL ↔ Arbitrum USDC, deBridge, LayerZero OFT(USDT0)

공개 API만 쓴다 (인증 없음, 누구나 같은 결과). Python 3.10+ 표준 라이브러리만 사용 — 설치할 것 없음.

| 폴더 | 역할 |
|---|---|
| `hyperliquid-tracer/` | BTC UTXO BFS + HyperUnit 매칭 → HL 계정 ([README](hyperliquid-tracer/README.md)) |
| `hl_ledger/` | HL 계정 원장·체결 → 입출금 표 + 1:1 판정 ([README](hl_ledger/README.md)) |
| `pipeline/` | 위 둘을 잇고 체인 공통 엔진(`engine.py`) + 체인 어댑터(`adapters.py`)로 체인 단계 ↔ 브리지 해석(`bridges.py`)을 반복(`flow.py`) |

**사례 3건 결과 요약은 [FINDINGS.md](FINDINGS.md).**

## 실행 (저장소 루트에서)

```bash
python3 -m pipeline --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37 --out out/case2 --hl-hops 2
python3 -m pipeline --tx 4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83 --out out/case1
python3 -m pipeline --start ethereum:0x49D52e55FA049878446712C4D7E59fc07106DD19 --out out/x   # 아무 체인 주소에서 시작
python3 -m pipeline --start arbitrum:0x… tron:T… --out out/x                                     # 여러 체인 주소 동시에
python3 -m pipeline --trace hyperliquid-tracer/output/traces/<시작>.json --out out/x   # 저장된 BTC 추적 재사용
python3 -m pipeline --hl 0x3A37880ff9EbD7B45376Ef15bDA69Cefc0016575 --out out/x         # HL 계정부터
python3 -m pipeline ... --reuse                                                         # 받아둔 HL·ETH 원자료 재사용
python3 -m pipeline ... --eth-hops -1                                                   # Ethereum 단계 생략
```

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--max-depth` | 7 | BTC 추적 최대 깊이 |
| `--hl-hops` | 1 | HL 계정 간 송금을 따라갈 단계 수 (0이면 BTC로 찾은 계정만) |
| `--max-accounts` | 50 | 조회할 HL 계정 상한. 넘친 송금은 리드로 남음 |
| `--start` | - | `<체인>:<주소>` (ethereum·arbitrum·base·optimism·polygon·tron). 들어온 자금 전체를 추적 대상으로 시작 |
| `--eth-hops` | 2 | EVM 체인에서 따라갈 단계 수 (0이면 도착 주소만, -1이면 EVM 단계 생략) |
| `--max-eth` | 30 | EVM 체인별 조회할 주소 상한 |
| `--no-bridges` | - | 브리지 목적지 조회 생략 |
| `--tron-hops` | 2 | Tron 도착 주소에서 따라갈 단계 수 (-1이면 Tron 단계 생략) |
| `--max-tron` | 30 | 조회할 Tron 주소 상한 |

출력 (`--out` 폴더):

| 파일 | 내용 |
|---|---|
| `pipeline.md` | 요약 + mermaid 흐름도 + 단계별 연결 + BTC 매칭 + HL 계정 판정 + HL 송금 + Ethereum 추적 + 다음 체인 리드 |
| `leads.csv` | 파이프라인이 더 따라가지 않은 목적지 (거래소·브리지 입금, 미추적 주소, 허브, 잔액, SOL 출금 등) — 다음 추적 입력 |
| `pipeline.json` | 위 내용 전체 (기계 판독용) |
| `btc/trace.json` | BTC 추적 원본 (hyperliquid-tracer 형식) |
| `hl/<계정>/` | 계정별 `report.md` · `events.csv` · `fills.csv` · `raw.json` (hl_ledger 형식) |
| `eth/<주소>/raw.json` | ETH 주소별 Blockscout 원자료 (`--reuse`로 재분석) |

## 연결 확실도

| 구간 | 확실도 결정 |
|---|---|
| BTC 시작점 → HyperUnit 입금 | 금액·상태·protocol 주소 일치 시 `confirmed`, 경로에 다중 입력 tx가 있으면 `estimated` |
| HL 계정 내부 (BTC로 찾은 계정) | hl_ledger 판정: `확정` / `계정 단위 확정` / `추정` |
| HL 송금으로 따라간 계정 | 추적 자금 외 유입이 1% 미만이면 `확정`(1건) · `계정 단위 확정`(여러 건), 아니면 `추정` |
| HL → 외부 체인 | 출금 nonce ↔ HyperUnit operation으로 출금 자체는 확정 |
| Ethereum 주소 → 주소 | 추적 자금 외 유입 + 추적 입금 이전 잔액이 1% 미만이면 `확정`/`계정 단위 확정`, 아니면 `추정` |

**경로 확실도** = 시작점부터 그 지점까지 구간 확실도 중 가장 낮은 것.

**허브 감지**: 송금으로 따라간 계정이 유입 50건 이상이고 추적 자금 비중이 1% 미만이면 브리지·거래소·서비스 지갑으로 보고 거기서 멈춘다.
그 계정의 유출은 남의 자금과 섞여 있어 리드로 쓰지 않는다.

## 구조: 체인 공통 엔진 + 어댑터

체인 하나 추가 = 어댑터 하나. 추적·판정·재판정·리드는 모든 체인이 `engine.py`를 같이 쓴다.

| 파일 | 역할 |
|---|---|
| `engine.py` | 주소 BFS, 연결 판정(추적 자금 외 유입 1%), 갈래 합류 재판정, 스왑·스테이킹·환불 이어 붙이기, 주소 오염 제외, 입금 주소·잔액 리드 |
| `adapters.py` | 체인별 원자료 → 전송·태그·규칙. EVM은 `blockscout.EVM_CHAINS`에 Blockscout 주소·기본 자산·토큰 화이트리스트 한 줄로 추가 |
| `flow.py` | HL 단계(또는 `--start`) → [체인 단계들 → 브리지 해석] 반복. 다른 단계에서 넘어온 리드만 다음 시작점 |
| `eth.py`·`tron.py` | 예전 이름 유지용 얇은 래퍼 (Tron 원자료 수집은 `tron.py`) |

체인별 규칙 차이 (어댑터 설정): Tron은 수수료 대납 스마트 계정 때문에 컨트랙트에서 멈추지 않고, 혼합 주소(추적 자금이 유입의 절반 이하)에서 멈춘다.
EVM은 태그 없는 컨트랙트에서 멈춘다. HL → Arbitrum USDC 출금은 수수료(1 USDC)를 뺀 금액·시각으로 도착을 찾는다.

## EVM 체인 추적

데이터는 Blockscout v2 공개 API(키 없음, 초당 약 10회)의 일반 tx · internal tx · 토큰 전송.
Etherscan 호환 `/api`는 키 없이 시간당 10회라 쓰지 않는다.

| 처리 | 이유 (실데이터에서 확인) |
|---|---|
| HyperUnit 출금은 internal tx로 들어온다 | Unit Treasury가 배치 컨트랙트(`0x4bbe9b…`)를 호출해 ETH를 보낸다. tx 해시로 매칭하고, Unit 기록에 tx가 없으면 금액(±0.5%)·시각(±12h)으로 찾는다 |
| 토큰은 USDC/USDT/WETH/DAI 컨트랙트만 | 심볼 "ETH"인 가짜 ERC-20이 대량으로 찍히고 평판도 "ok"로 나온다 |
| 주소 오염 제외 | 출금 주소마다 앞·뒤 4자리가 닮은 주소의 먼지 입금이 수백 건. 유입에서 빼고 건수만 센다. 그 주소로 실제 송금하면 `오염 피해 의심` 표시 |
| 이력 전체 수집 | v2 API는 최신순이라 스팸을 다 넘겨야 추적 시점에 닿는다 (엔드포인트당 6,000건 상한) |
| DEX 스왑은 전환 | ETH를 CoW Swap에 넣고 USDT를 돌려받는 식. 스왑 대금 수령을 추적 자금으로 잇는다 |
| 스테이킹은 예치 | `EthBatchDepositor` 등에 넣은 ETH는 몇 달 뒤 원금+보상으로 돌아온다. 비콘 체인 출금 기록(`/withdrawals`, 최근 500건)을 받아 회수액을 추적 자금으로 센다. 조회가 실패하면 이후 유출로 이어 추적 |
| 태그 없는 거래소 입금 주소 | 이전 잔액 없이 받은 추적 자금의 95% 이상을 72시간 안에 거래소 핫월렛으로 보낸 주소는 그 거래소 입금 주소로 표시 |
| 되돌아온 자금·환불 | A → B → A, 브리지 주문 취소 환불은 혼입이 아니라 추적 자금으로 센다 |
| 종착 | Blockscout 태그로 거래소·브리지(USDT0 OFT 포함)·믹서 판정, 그 외 컨트랙트, 이력 상한을 넘는 허브 |

## 브리지 목적지 (`pipeline/bridges.py`)

브리지 입금 리드를 목적 체인 도착 리드로 바꾼다. 공개 API (인증 없음):

| 브리지 | 조회 | 얻는 것 |
|---|---|---|
| deBridge (DLN) | `stats-api.dln.trade` tx → orderId → 주문 | 목적 체인, 받는 주소, 도착 금액·토큰, 도착 tx. 취소 주문은 출발 주소 환불로 표시 |
| LayerZero OFT (USDT0 등) | `scan.layerzero-api.com` tx → 메시지 | 목적 체인, 도착 tx. 받는 주소·금액은 OFT payload 끝 40바이트에서 해석 (Tron은 base58check) |

## Tron 추적 (`pipeline/tron.py`)

브리지 도착 Tron 주소에서 USDT(TRC-20) 흐름을 따라간다. 데이터는 TronScan 공개 API `token_trc20/transfers` (키 없음).

| 처리 | 이유 (실데이터에서 확인) |
|---|---|
| 토큰 필터 없이 받고 USDT만 직접 거름 | `contract_address` 필터를 붙이면 TronScan이 total을 항상 10,000으로 돌려준다 (실제 26건이어도) |
| 컨트랙트 주소라도 멈추지 않음 | 수수료 대납(건당 1.5 USDT) 스마트 계정도 컨트랙트로 나온다. 지갑처럼 쓰인다 |
| 혼합 주소에서 멈춤 | 추적 자금이 유입의 절반 이하인 주소의 유출을 따라가면 리드가 남의 돈으로 불어난다 (사례 1에서 181건 → 59건) |
| 모이는 곳 순위 | 추적 주소 2개 이상이 보낸 주소를 보고서에 순위로 표시 |
| 거래소 판정은 태그가 있을 때만 | 키 없이는 TronScan 주소 태그가 거의 비어 있다 |

## 허브 판정 보강

HL 허브가 감지되면 그 허브로 토큰을 보낸 주소가 **토큰 발행자(deployer)**인지 HL `tokenDetails`로 확인한다.
발행자가 재고를 채워주는 지갑이면 그 토큰 서비스의 운영 지갑으로 표시한다 (사례 2: XMR1 · Wagyu.xyz).

## 보고서 구성

`pipeline.md` 맨 위에 **한눈에 보기**(단계별로 묶은 흐름도: 시작 → BTC 입금 → HL 계정 → ETH 주소 → 도착지별 건수·금액·경로 확실도)가 있고,
그 아래 계정·주소 단위 상세 흐름도와 표가 이어진다.

## 실데이터 결과 (2026-10-03)

**사례 2** `bc1ql4u9…hf6f37`, `--hl-hops 2`, 약 1분

- BTC: output 39개, HyperUnit 매칭 5건 → HL 계정 `0x3A37…6575` (BTC 구간 estimated)
- HL: 계정 단위 확정 (Unit 입금 18건, BTC 추적이 닿은 건 5건 = 입금량의 29.7%) → XMR1 23건을 23개 계정으로 송금
- hop 1: 23개 계정 전부 "추적 자금 1건만 받음 → 확정", 그리고 전부 같은 주소 `0x048b75…c05eb3`로 XMR1을 다시 모음
- hop 2: `0x048b75…`는 원장 8,502건, 유입 1.78억 USDC 규모 → 허브 의심, 추적 종료

**사례 1** `4695e212…8a83` (BTC tx), 기본 옵션, 약 1분

- BTC: output 65개, HyperUnit 매칭 24건 → HL 계정 24개 (입금마다 새 계정, 각 5 BTC 전후)
- HL: 24개 전부 `확정` (Unit 입금 1건이 유일한 자금원) → 24개 전부 UETH를 HyperUnit으로 Ethereum 출금
- 경로 확실도: ETH 출금 리드 22건 `확정`, 2건 `추정` (BTC 경로에 다중 입력 tx)
- 형제 계정끼리 주고받은 소액 USDC 송금 6건, HyperEVM 전송 1건 (1 USDC)
- Ethereum: 출금 목적지 5개 → ETH 주소 21개 (hop 0·1·2). 출금 주소마다 주소 오염 먼지 271~2,927건을 걸러냄
  - 전형적 경로: 새 주소에서 300~600 ETH씩 쪼개기 → CoW Swap으로 ETH→USDT → **USDT0(OFT) 브리지**로 다른 체인
  - 일부는 ETH 그대로 **deBridge**로 다른 체인
  - 브리지 19건 전부 **Tron** 도착: USDT0 11건 10.9M USDT0, deBridge 7건 5.2M USDT (수취 주소 14개). deBridge 1건은 취소·환불 후 USDT0로 재전송
  - 출금 주소 3개는 BTC 추적 범위 밖 다른 HL 계정의 Unit 출금이 같이 들어와 `추정` (사례 1 전체 79계정 중 BTC 추적이 닿은 건 24개)
- 소요: BTC 추적 1~10분 (Esplora 공개 API 상태에 따라), HL 수 초, ETH 주소당 수십 초 (`--reuse`면 전체 3초)

**HyperEVM 브리지**: `0x2222…2222`(HYPE)와 `0x20…00 + 토큰 인덱스`(현물 토큰)는 계정이 아니라 HyperCore → HyperEVM 전송 주소다.
따라가지 않고 `hyperevm` 리드(수신 주소 = 보낸 계정과 같은 주소)로 남긴다.

## 테스트 (오프라인)

```bash
./run_tests.sh      # pipeline · hl_ledger · hyperliquid-tracer 전부 (네트워크 없음)
```

## 한계

- 주소 공통성·그래프 연결은 소유권·불법성의 증명이 아니다. "이 자금이 어디로 갔는가"만 다룬다.
- BTC 추적은 깊이·fan-in/out 30·tx 500개 제한이 있어 HL 계정의 Unit 입금 전부에 닿지 않을 수 있다 (보고서에 비율 표시).
- Ethereum은 ETH와 주요 스테이블 4종만 본다. 거래소 판정은 공개 태그 기준이라 태그 없는 거래소 입금 주소는 일반 주소로 한 홉 더 따라간다.
- Tron은 USDT만, 거래소 판별은 태그가 있을 때만. Solana·Arbitrum·HyperEVM 도착 이후는 리드로만 남긴다.
- HyperEVM은 키 없는 탐색기 API가 없어 리드로만 남는다 (Etherscan v2 무료 키가 있으면 EVM 어댑터로 붙일 수 있다).
- BSC는 Blockscout 공개 인스턴스가 없다. Solana·Bitcoin(출금 방향)도 아직 어댑터가 없다.
- 브리지 해석은 deBridge·LayerZero OFT만. Socket·LI.FI·Across 등은 브리지 입금으로 판별만 하고 목적지는 리드로 남는다.
- HL은 오래된 체결을 돌려주지 않는다. 대량 거래 계정은 `기록 누락 의심` 표식을 확인할 것.
