# tracker — BTC → HyperUnit → Hyperliquid 자금 흐름 추적

BTC tx나 주소 하나를 넣으면 **UTXO 소비 관계 → HyperUnit 입금 → HL 계정 내부 매매 → HL 계정 간 송금 → 외부 체인 출금**까지
한 번에 따라가고, 구간마다 연결 확실도를 붙여 보고서로 뽑는다.

공개 API만 쓴다 (인증 없음, 누구나 같은 결과). Python 3.10+ 표준 라이브러리만 사용 — 설치할 것 없음.

| 폴더 | 역할 |
|---|---|
| `hyperliquid-tracer/` | BTC UTXO BFS + HyperUnit 매칭 → HL 계정 ([README](hyperliquid-tracer/README.md)) |
| `hl_ledger/` | HL 계정 원장·체결 → 입출금 표 + 1:1 판정 ([README](hl_ledger/README.md)) |
| `pipeline/` | 위 둘을 잇고 HL 송금 홉·다음 체인 리드까지 정리 |

## 실행 (저장소 루트에서)

```bash
python3 -m pipeline --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37 --out out/case2 --hl-hops 2
python3 -m pipeline --tx 4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83 --out out/case1
python3 -m pipeline --trace hyperliquid-tracer/output/traces/<시작>.json --out out/x   # 저장된 BTC 추적 재사용
python3 -m pipeline --hl 0x3A37880ff9EbD7B45376Ef15bDA69Cefc0016575 --out out/x         # HL 계정부터
python3 -m pipeline ... --reuse                                                         # 받아둔 HL 원자료 재사용
```

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--max-depth` | 7 | BTC 추적 최대 깊이 |
| `--hl-hops` | 1 | HL 계정 간 송금을 따라갈 단계 수 (0이면 BTC로 찾은 계정만) |
| `--max-accounts` | 50 | 조회할 HL 계정 상한. 넘친 송금은 리드로 남음 |

출력 (`--out` 폴더):

| 파일 | 내용 |
|---|---|
| `pipeline.md` | 요약 + mermaid 흐름도 + 단계별 연결 + BTC 매칭 + HL 계정 판정 + 송금 + 다음 체인 리드 |
| `leads.csv` | 파이프라인이 더 따라가지 않은 목적지 (ETH/SOL 출금 주소·tx, 미추적 HL 계정, 허브) — 다음 체인 추적 입력 |
| `pipeline.json` | 위 내용 전체 (기계 판독용) |
| `btc/trace.json` | BTC 추적 원본 (hyperliquid-tracer 형식) |
| `hl/<계정>/` | 계정별 `report.md` · `events.csv` · `fills.csv` · `raw.json` (hl_ledger 형식) |

## 연결 확실도

| 구간 | 확실도 결정 |
|---|---|
| BTC 시작점 → HyperUnit 입금 | 금액·상태·protocol 주소 일치 시 `confirmed`, 경로에 다중 입력 tx가 있으면 `estimated` |
| HL 계정 내부 (BTC로 찾은 계정) | hl_ledger 판정: `확정` / `계정 단위 확정` / `추정` |
| HL 송금으로 따라간 계정 | 추적 자금 외 유입이 1% 미만이면 `확정`(1건) · `계정 단위 확정`(여러 건), 아니면 `추정` |
| HL → 외부 체인 | 출금 nonce ↔ HyperUnit operation으로 출금 자체는 확정 |

**경로 확실도** = 시작점부터 그 지점까지 구간 확실도 중 가장 낮은 것.

**허브 감지**: 송금으로 따라간 계정이 유입 50건 이상이고 추적 자금 비중이 1% 미만이면 브리지·거래소·서비스 지갑으로 보고 거기서 멈춘다.
그 계정의 유출은 남의 자금과 섞여 있어 리드로 쓰지 않는다.

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

**HyperEVM 브리지**: `0x2222…2222`(HYPE)와 `0x20…00 + 토큰 인덱스`(현물 토큰)는 계정이 아니라 HyperCore → HyperEVM 전송 주소다.
따라가지 않고 `hyperevm` 리드(수신 주소 = 보낸 계정과 같은 주소)로 남긴다.

## 테스트 (오프라인)

```bash
python3 -m pytest pipeline/tests -q                                  # 파이프라인 연결
(cd hl_ledger && python3 -m pytest -q)                               # hl_ledger 정답 검증
(cd hyperliquid-tracer && python3 -m unittest discover -s tests && python3 tests/verify_saved_cases.py)
```

## 한계

- 주소 공통성·그래프 연결은 소유권·불법성의 증명이 아니다. "이 자금이 어디로 갔는가"만 다룬다.
- BTC 추적은 깊이·fan-in/out 30·tx 500개 제한이 있어 HL 계정의 Unit 입금 전부에 닿지 않을 수 있다 (보고서에 비율 표시).
- 외부 체인(ETH/SOL) 출금 이후는 리드로만 남긴다. ETH 추적은 `hyperliquid-tracer/clients/ethereum.py` adapter까지만 있음.
- HL은 오래된 체결을 돌려주지 않는다. 대량 거래 계정은 `기록 누락 의심` 표식을 확인할 것.
