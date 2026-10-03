# BAY Hyperliquid Tracer

Bitcoin의 **UTXO/outpoint 소비 관계**를 BFS로 따라가고 HyperUnit operation을 조회하여 BTC 입금과 Hyperliquid 계정을 매칭합니다. Python 3.10 이상, 표준 라이브러리만 사용합니다.

## 실행

작업 폴더에서 실행합니다. 별도 dependency 설치는 필요 없습니다.

```bash
python main.py --tx 4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83
python main.py --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37
python main.py --tx 4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83 --max-depth 7 --format csv --output output/case1.csv
```

기본 실행은 JSON 전체 graph와 CSV match를 **둘 다** 저장합니다.

- `output/traces/{시작 tx 또는 주소}.json`
- `output/matches/{시작 tx 또는 주소}.csv`

`--output`은 `--format`으로 선택한 파일의 경로를 바꾸며 다른 형식은 기본 위치에 저장합니다. BTC 표시는 정확한 소수점 8자리 문자열이고 계산·합계는 satoshi 정수입니다. CSV의 입금액은 해당 output의 전체 금액입니다.

`.env.example`을 `.env`로 복사하고 필요할 때 provider를 설정합니다. 기존 환경변수가 `.env`보다 우선합니다.

```text
ESPLORA_BASE_URL=https://blockstream.info/api
HYPERUNIT_BASE_URL=https://api.hyperunit.xyz
ETH_RPC_URL=https://your-ethereum-json-rpc-provider
```

Esplora는 `https://mempool.space/api`로도 변경할 수 있습니다. HTTP timeout은 15초, 재시도는 최대 2회이며 성공한 응답을 실행 내에서 캐시합니다. 공개 API 속도·제한에 따라 수 분 걸릴 수 있습니다. HTTP 429/5xx와 연결 오류를 재시도합니다.

## 추적 및 매칭 규칙

- 시작 tx output의 depth는 0이며 최대 depth는 기본 7입니다.
- 주소 입력은 주소로 지급된 output만 seed로 사용합니다. 무관한 change output은 seed가 아닙니다.
- 주소 이력은 confirmed pagination을 적용하며 최대 20페이지입니다. 한도를 넘으면 에러를 내어 부분 seed를 완전한 이력으로 취급하지 않습니다.
- outspends로 다음 tx를 찾고 해당 tx 입력이 실제 parent outpoint를 소비하는지도 확인합니다.
- input/output 각 30개를 초과하면 해당 tx branch를 중단합니다. 실행당 tx 500개로 추가 제한합니다.
- stop reason은 `max_depth`, `high_fanout`, `high_fanin`, `unspent`, `unit_found`, `already_visited`, `api_error`, `max_transactions`입니다.
- JSON에는 output node, 소비 edge, tx 단위 stop, 오류, 원본 operation 및 HL 계정별 입금 합계가 들어갑니다. 합계는 발견된 입금 output 전체 금액이며 시작 자금의 귀속 금액은 아닙니다.
- visited tx와 outpoint로 중복 탐색을 막습니다. 합류 경로는 edge에 기록되지만 각 tx의 output은 최초 도달 경로를 기준으로 한 번 저장합니다.
- HyperUnit의 실제 `sourceTxHash`는 `txid:vout` 형식입니다. txid와 output 번호, protocol address, source chain을 검증합니다. txid만 제공되는 응답도 지원합니다.
- `confirmed`: HL destination chain/계정 주소, done state, sourceAmount와 output 값 일치, 실제 UTXO 연결을 확인하고 경로 중 merge가 없는 경우.
- `estimated`: 위 protocol 매칭은 확인됐지만 경로 중 다중 입력 tx가 있어 자금 귀속이 불확실한 경우.
- `unresolved`: source와 protocol은 일치하지만 완료 상태·대상·금액 검증이 부족한 경우.
- `protocol_match_confirmed`와 `attribution_uncertain`은 별도 필드입니다. 주소 공통성이나 graph 연결 자체는 소유권·불법성·동일 자금의 증명이 아닙니다. taint/purity 계산은 구현하지 않았습니다.
- HyperUnit API 오류가 나면 오류를 기록하고 Bitcoin 소비 관계 탐색은 계속합니다. BTC API 오류는 해당 branch만 중단합니다. 오류가 있을 때 결과는 부분 조회입니다.
- CLI exit code: 정상 0, 시작 조회 실패/유효한 output 없음 1, output은 있지만 API 오류 발생 2.

## 실제 검증 결과 (2026-10-02)

두 명령을 공개 API로 실제 실행하고 저장 결과의 acceptance 조건을 검사했습니다.

| 사례 | output 수 | match 수 | 핵심 입금 | HL 계정 | 결과 |
|---|---:|---:|---|---|---|
| Case 1 | 65 | 24 | `7a8b75339e92a254caf181884875c9b76d763bcda25795ba9159e25d5d14b123`, 499997500 sats, depth 6 | `0xC18d9Dea73f7D8E8D4F9a5fAD69849F7319C3553` | confirmed |
| Case 2 | 39 | 5 | `685ea58a17e8fa27fe99acc59e41dd4b011024dc6304f12c68a9785734256a2f`, 633400 sats, depth 4 | `0x3A37880ff9EbD7B45376Ef15bDA69Cefc0016575` | estimated, protocol 매칭 확인 |

두 실행 모두 API 오류 0건입니다. Case 2의 대표 4개 tx 사이 소비 edge도 모두 확인했습니다. 18건 전체 복원은 이 MVP의 acceptance 조건이 아니며 기본 depth 7 실행에서 5건을 발견했습니다.

HyperUnit 응답의 `destinationTxHash`는 요청에 제공된 HL explorer hash와 달리 `0x주소:timestamp` 형태였습니다. 이를 explorer hash로 변환하거나 동일하다고 가정하지 않고 원본 operation에 보존합니다.

## 테스트

```bash
python -m unittest discover -s tests -v
python tests/verify_saved_cases.py
```

12개 offline 테스트는 UTXO 연결, merge, depth/fan-in 제한, source tx/output 검증, chain/금액 불일치, API 오류 처리, 캐시, Ethereum RPC method를 검증합니다. `verify_saved_cases.py`는 함께 저장된 실제 결과의 두 acceptance 조건을 검사하며 새로운 API 요청을 하지 않습니다.

네트워크 integration 테스트 2개는 기본 skip이며 새 데이터를 검증하려면 PowerShell에서 실행합니다.

```powershell
$env:RUN_LIVE_TESTS = '1'
python -m unittest discover -s tests -p test_live_cases.py -v
```

## Ethereum adapter

```python
from clients.ethereum import EthereumClient
client = EthereumClient()  # .env의 ETH_RPC_URL
transaction = client.get_eth_tx(tx_hash)
receipt = client.get_eth_receipt(tx_hash)
```

provider 중립 JSON-RPC의 `eth_getTransactionByHash`와 `eth_getTransactionReceipt`를 사용합니다. provider의 not-found 응답은 `None`이고 RPC error는 `APIError`입니다. RPC credential은 코드에 넣지 않습니다.

제공된 ETH tx를 공개 RPC로 조회한 결과 transaction은 조회됐지만 receipt는 `null`이었습니다. tx의 직접 `to`는 `0x4bbe9b84aac9804557e8a90b7186324f20357e5c`, 직접 value는 약 158.190919 ETH였습니다. 따라서 요청의 `0xFAA99D6314D7ceA16Ccb5F6f9bDA6De8b1247B74`에 대한 약 158.1667 ETH 수취는 이 기본 adapter로 검증되지 않았습니다. Internal transfer/trace 조회와 HL 출금 연결은 후속 범위입니다.

## 구조

`clients/`: HTTP, Bitcoin, HyperUnit, Ethereum API. `tracing/`: BFS 엔진과 operation matcher. `models/`: 결과와 정확한 BTC 표시. `main.py`: CLI·JSON/CSV 저장. `config.py`: 설정·환경변수. `tests/`: offline·선택적 live 검증.
