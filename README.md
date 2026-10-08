# tracker: 크로스체인 자금 흐름 추적

BTC tx나 주소 하나를 넣으면 Hyperliquid, Ethereum, Tron을 건너가는 자금을 끝까지 따라가고, 구간마다 **왜 이어졌는지(근거)**와 **얼마나 확실한지(확실도)**를 붙여 보고서로 낸다.
거래소, 서비스 지갑, 미지원 체인에 닿으면 멈추고 그 지점을 다음 추적 리드로 남긴다.

- 체인: Bitcoin · Hyperliquid · Ethereum · Arbitrum · Base · Optimism · Polygon · Tron
- 체인 사이: HyperUnit (BTC/ETH/SOL ↔ HL) · HL ↔ Arbitrum USDC · deBridge · LayerZero OFT (USDT0)
- 공개 API만 사용 (인증 없음, 누구나 같은 결과) · Python 3.10+ 표준 라이브러리만, 설치할 것 없음

## 결과

실제 사례 3건을 공개 데이터로 끝까지 추적했다 (조회 2026-10-03). 자세한 내용은 [FINDINGS.md](FINDINGS.md).

| 사례 | 시작 | 경로 | 추적이 멈춘 곳 |
|---|---|---|---|
| 1 | BTC tx `4695e212…` | BTC 120개 → HL 계정 24개 → ETH 주소 22개 → CoW 스왑 · USDT0/deBridge 브리지 20건 | **Tron** 수취 주소 15개, 약 1,670만 USDT |
| 2 | BTC 주소 `bc1ql4u9…` | BTC → HL 계정 1개 → XMR1 매수 → 계정 23개로 분산 → 한 지갑으로 재집결 | **Wagyu.xyz** 서비스 지갑 (모네로 전환 추정) |
| 3 | HL 계정 `0x7c4399…` | HL → ETH 244,860개 → 스테이킹 3개월 (보상 1,501 ETH) → 주소 이동 | **Binance** 핫월렛 (마지막 구간 추정) |

세 사례 모두 체인을 바꾼 지점에서는 추적이 끊기지 않았다. 멈춘 곳은 거래소와 프라이버시 코인이었다.
입금마다 새 계정을 쓰는 식의 분산은 오히려 계정마다 자금원을 하나로 만들어 1:1 연결을 확정시켰다 (사례 1의 24개 계정 전부 `확정`).

**그래프로 보기**: `python3 viewer/build.py` → `viewer/index.html`을 브라우저로 연다. 노드를 누르면 시작점까지의 경로, 판정 근거, 거래 링크가 나온다 ([viewer/](viewer/README.md)).

## 체인 사이를 잇는 방법

체인이 바뀌면 tx hash는 이어지지 않는다. 대신 브리지·프로토콜이 남긴 기록을 연결고리로 쓴다.

| 구간 | 연결고리 | 확실도 |
|---|---|---|
| BTC → HL 입금 | UTXO 소비 관계 BFS → HyperUnit operation의 `sourceTxHash` ↔ HL 계정 | 금액·상태·protocol 주소 일치 시 확정 |
| HL 계정 내부 | 원장·체결로 입금 ↔ 매매 ↔ 출금 수지 대조 | 확정 / 계정 단위 확정 / 추정 |
| HL → 외부 체인 | HL 원장 출금 `nonce` = HyperUnit 출금 기록의 nonce | 출금 자체는 확정 |
| 주소 → 주소 | 추적 자금 외 유입 + 추적 이전 잔액이 1% 미만인가 | 확정 / 계정 단위 확정 / 추정 |
| 브리지 → 목적 체인 | deBridge 주문 ID, LayerZero 메시지 payload (받는 주소·금액은 OFT payload 끝 40바이트) | 도착 주소·금액·tx |

| 확실도 | 조건 |
|---|---|
| `확정` | 자금원이 추적 자금 1건뿐 (그 외 유입 1% 미만) |
| `계정 단위 확정` | 추적 자금 여러 건이 한 계정에 합쳐짐. 계정 출금 전체는 여기서 나왔지만 건별 매핑은 추정 |
| `추정` | 다른 자금이 1% 이상 섞임 |

**경로 확실도**는 시작점부터 그 지점까지 구간 중 가장 낮은 값이다. DEX 스왑, 스테이킹 회수(비콘 체인 출금), 브리지 환불은 종착이 아니라 같은 자금의 전환으로 이어 붙인다.

## 검증

팀원이 손으로 정리한 사례 표와 대조했다 ([hl_ledger 보고서](hl_ledger/README.md)).

- 사례 1 HL 계정 79개, 입금 360.54 BTC, ETH 출금 69건 · SOL 출금 11건 전부 일치
- 사람이 나눈 계정 분류(정상 67 · 잔량 7 · 소액 유입 4 · 재매수 1)가 같은 계정으로 똑같이 나옴
- HyperUnit 기록 ↔ HL 원장 nonce 219/219 일치, 짝 없는 기록 0건
- HL API가 오래된 체결을 돌려주지 않는 계정을 수지 검산으로 찾아 `기록 누락 의심` 표시 (800 BTC 계정: 매도 기록이 입금의 37%)

```bash
./run_tests.sh      # pipeline · hl_ledger · hyperliquid-tracer 정답 검증 (오프라인)
```

## 실행

저장소 루트에서:

```bash
python3 -m pipeline --tx 4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83 --out out/case1
python3 -m pipeline --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37 --out out/case2 --hl-hops 2
python3 -m pipeline --hl 0x7c4399A2E5752A57703391da290ce684C12eCb43 --out out/case3 --eth-hops 4
python3 -m pipeline --start ethereum:0x49D52e55FA049878446712C4D7E59fc07106DD19 --out out/x   # 아무 체인 주소에서
python3 -m pipeline ... --reuse                                                              # 받아둔 원자료로 재분석 (수 초)
```

처음 실행은 공개 API 속도에 따라 사례당 1~10분.

<details>
<summary>옵션 전체</summary>

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--tx` / `--address` | - | BTC tx 또는 주소에서 시작 |
| `--hl` | - | HL 계정에서 시작 |
| `--start` | - | `<체인>:<주소>` (ethereum·arbitrum·base·optimism·polygon·tron), 여러 개 가능. 들어온 자금 전체를 추적 대상으로 |
| `--trace` | - | 저장된 BTC 추적 JSON 재사용 |
| `--max-depth` | 7 | BTC 추적 최대 깊이 |
| `--hl-hops` | 1 | HL 계정 간 송금을 따라갈 단계 수 (0이면 BTC로 찾은 계정만) |
| `--max-accounts` | 50 | 조회할 HL 계정 상한. 넘친 송금은 리드로 남음 |
| `--eth-hops` | 2 | EVM 체인에서 따라갈 단계 수 (0이면 도착 주소만, -1이면 EVM 단계 생략) |
| `--max-eth` | 30 | EVM 체인별 조회할 주소 상한 |
| `--tron-hops` | 2 | Tron에서 따라갈 단계 수 (-1이면 생략) |
| `--max-tron` | 30 | 조회할 Tron 주소 상한 |
| `--no-bridges` | - | 브리지 목적지 조회 생략 |
| `--reuse` | - | 받아둔 HL·ETH 원자료 재사용 |

</details>

출력 (`--out` 폴더):

| 파일 | 내용 |
|---|---|
| `pipeline.md` | 한눈에 보기 흐름도 + 구간별 연결 근거 + 계정·주소 상세 + 다음 체인 리드 |
| `leads.csv` | 추적을 멈춘 지점 (거래소·브리지 입금, 허브, 미추적 주소, 미지원 체인 출금). 다음 추적 입력 |
| `pipeline.json` | 위 내용 전체 (뷰어 입력) |
| `btc/` · `hl/<계정>/` · `eth/<주소>/` | 체인별 원자료 (`--reuse`로 재분석) |

## 구조

체인 하나 추가 = 어댑터 하나. 추적·판정·리드는 모든 체인이 `engine.py`를 같이 쓴다.

| 폴더 / 파일 | 역할 |
|---|---|
| `hyperliquid-tracer/` | BTC UTXO BFS + HyperUnit 매칭 → HL 계정 ([README](hyperliquid-tracer/README.md)) |
| `hl_ledger/` | HL 계정 원장·체결 → 입출금 표 + 1:1 판정 ([README](hl_ledger/README.md)) |
| `pipeline/engine.py` | 주소 BFS, 연결 판정, 갈래 합류 재판정, 스왑·스테이킹·환불 이어 붙이기, 주소 오염 제외, 리드 |
| `pipeline/adapters.py` | 체인별 원자료 → 전송·태그·규칙. EVM 체인은 Blockscout 주소·기본 자산·토큰 목록 한 줄로 추가 |
| `pipeline/bridges.py` | deBridge · LayerZero OFT 브리지 입금 → 목적 체인 도착 |
| `pipeline/flow.py` | HL 단계(또는 `--start`) → [체인 단계 → 브리지 해석] 반복 |
| `viewer/` | 결과 그래프 뷰어 (정적 페이지) |
| `deck/` | 발표자료 (Markdown → PDF, [README](deck/README.md)) |

## 실데이터에서 만난 문제

공개 데이터를 그대로 쓰면 결과가 틀린다. 각각 따로 처리했다.

| 문제 | 실제로 본 것 | 처리 |
|---|---|---|
| 주소 오염 | 출금 주소마다 앞뒤 4자리가 닮은 주소의 먼지 입금 271~2,927건 | 유입에서 빼고 건수만 셈. 그 주소로 실제 송금하면 `오염 피해 의심` |
| 가짜 토큰 | 심볼 "ETH"인 ERC-20이 대량, 평판도 "ok" | USDC · USDT · WETH · DAI 컨트랙트만 |
| 숨은 입금 | HyperUnit 출금은 배치 컨트랙트의 internal tx로 들어옴 | tx 해시로 매칭, 없으면 금액(±0.5%)·시각(±12h) |
| 스테이킹 | 예치한 ETH가 몇 달 뒤 비콘 체인 출금으로 돌아옴 (일반 tx 목록에 없음) | `/withdrawals`를 따로 받아 회수액을 추적 자금으로 |
| TronScan 응답 | 토큰 필터를 붙이면 total이 항상 10,000 (실제 26건이어도) | 필터 없이 받고 USDT만 직접 거름 |
| Tron 스마트 계정 | 수수료 대납 계정이 컨트랙트로 나오지만 지갑처럼 쓰임 | 컨트랙트에서 멈추지 않음 |
| 혼합 주소 | 남의 돈이 섞인 주소를 계속 따라가면 리드 폭증 (사례 1: 181건) | 추적 자금이 유입의 절반 이하면 멈춤 (59건) |
| 태그 없는 서비스 지갑 | 사례 2의 허브 지갑은 라벨이 없음 | 허브에 재고를 넣는 주소가 토큰 발행자인지 HL `tokenDetails`로 확인 (XMR1 → Wagyu.xyz) |
| 잘리는 체결 기록 | HL이 오래된 체결을 돌려주지 않음 | 입금 = 매도 + 유출 + 잔액 검산, 안 맞으면 `기록 누락 의심` |

<details>
<summary>그 밖의 판정 규칙</summary>

- **허브 감지**: 송금으로 따라간 계정이 유입 50건 이상이고 추적 자금 비중이 1% 미만이면 거래소·서비스 지갑으로 보고 멈춘다. 그 계정의 유출은 남의 자금과 섞여 있어 리드로 쓰지 않는다.
- **태그 없는 거래소 입금 주소**: 이전 잔액 없이 받은 추적 자금의 95% 이상을 72시간 안에 거래소 핫월렛으로 보낸 주소는 그 거래소 입금 주소로 표시한다.
- **되돌아온 자금**: A → B → A, 브리지 주문 취소 환불은 혼입이 아니라 추적 자금으로 센다.
- **HyperEVM 전송**: `0x2222…2222`(HYPE)와 `0x20…00 + 토큰 인덱스`는 계정이 아니라 HyperCore → HyperEVM 전송 주소. `hyperevm` 리드로 남긴다.
- **HL → Arbitrum USDC 출금**: 수수료 1 USDC를 뺀 금액·시각으로 도착을 찾는다.
- **EVM 데이터**: Blockscout v2 공개 API (키 없음, 초당 약 10회). 최신순이라 스팸을 다 넘겨야 추적 시점에 닿는다 (엔드포인트당 6,000건 상한). Etherscan 호환 `/api`는 키 없이 시간당 10회라 쓰지 않는다.

</details>

## 한계

- 주소 연결은 자금이 어디로 갔는지만 보여준다. 소유자나 불법성의 증명이 아니다.
- BTC 추적은 깊이 · fan-in/out 30 · tx 500개 제한이 있어 HL 계정의 입금 전부에 닿지 않을 수 있다 (보고서에 비율 표시).
- Ethereum은 ETH와 주요 스테이블 4종, Tron은 USDT만 본다. 거래소 판정은 공개 태그 기준.
- Solana · HyperEVM · BSC 도착 이후는 리드로만 남는다 (Solana 어댑터 없음, HyperEVM은 키 없는 탐색기 API 없음, BSC는 Blockscout 공개 인스턴스 없음).
- 브리지 목적지 해석은 deBridge · LayerZero OFT만. Across · LI.FI · Socket은 브리지 입금으로 판별만 한다.
- THORChain · Chainflip은 아직 지원하지 않는다 (다음 과제).

---

BAY 26-2 보안 2조 팀 프로젝트. `hyperliquid-tracer/`는 팀원 작업물을 그대로 가져와 연결했다.
