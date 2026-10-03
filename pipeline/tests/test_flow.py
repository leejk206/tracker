"""체인 공통 엔진·다중 체인 반복·시작점 오프라인 검증.

- HL 리드 → Ethereum 단계 → 브리지 해석 → Tron 단계로 이어지는가
- 체인 단계가 스스로 남긴 리드를 다음 라운드 시작점으로 다시 잡지 않는가 (끝없이 넓어지는 버그)
- Arbitrum 어댑터: 체인별 토큰 화이트리스트, HL → Arbitrum USDC 출금(수수료 1 USDC)을 금액·시각으로 찾기
- --start: 아무 체인 주소에서 시작해 들어온 자금 전체를 추적
"""
from decimal import Decimal

from pipeline import adapters, engine, flow
from pipeline.bridges import BridgeHop
from pipeline.core import Lead
from test_eth import A, BINANCE, SEED, SEED_TX, T0, iso, raw, seed_lead, tok, tx, unit_in

ARB_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
ETH_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
HL_BRIDGE = "0x2df1c51e09aecf9cacb7bc98cb1742757f163df7"
OFT = "0x1f748c76de468e9d11bd340fa9d5cbadf315dfb0"
TRON_R = "TMkyyM9RomoGLqQkoNfde2WLvcWi4W1aJk"
TRON_NEXT = "TNext111111111111111111111111111111"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
NEXT = "0x2222000000000000000000000000000000003333"


def tron_row(txid, frm, to, usdt, sec):
    return {"transaction_id": txid, "from_address": frm, "to_address": to, "quant": str(int(usdt * 10 ** 6)),
            "block_ts": int(T0.timestamp() * 1000) + sec * 1000, "contract_address": USDT_TRON, "contractRet": "SUCCESS"}


def tron_raw(addr, rows):
    return {"address": addr, "fetchedAt": 0, "transfers": rows, "tags": {}, "contracts": [], "total": len(rows),
            "truncated": False}


class FakeHL:
    """HL 단계 결과 흉내: 계정 하나가 Unit 출금 리드를 하나 가진다."""
    def __init__(self, leads):
        self.leads = leads


def counting(raws):
    calls = []

    def fetch(addr):
        calls.append(addr)
        return raws[addr.lower() if addr.startswith("0x") else addr]
    return fetch, calls


def test_hl_to_ethereum_to_bridge_to_tron():
    oft = A(OFT, "UsdtOFT", contract=True)
    eth_raws = {SEED: raw(SEED, internal=[unit_in(SEED, amount=300)],
                          tokens=[tok("0x" + "f" * 64, A(SEED), oft, "0xdac17f958d2ee523a2206206994597c13d831ec7",
                                      700000, 6, 30, 130)])}
    tron_raws = {TRON_R: tron_raw(TRON_R, [tron_row("dd", "Tbridge", TRON_R, 699000, 100),
                                           tron_row("ee", TRON_R, TRON_NEXT, 698000, 200)]),
                 TRON_NEXT: tron_raw(TRON_NEXT, [tron_row("ee", TRON_R, TRON_NEXT, 698000, 200)])}
    eth_fetch, eth_calls = counting(eth_raws)
    tron_fetch, tron_calls = counting(tron_raws)
    ads = {"ethereum": adapters.adapter_for("ethereum", eth_fetch), "tron": adapters.adapter_for("tron", tron_fetch)}

    def resolve(todo):
        assert [l.tx for l in todo] == ["0x" + "f" * 64]
        return {todo[0].tx: [BridgeHop("USDT0 (LayerZero)", todo[0].tx, SEED, "tron", TRON_R, "USDT0",
                                       Decimal(699000), "SUCCEEDED", "0xdd", int(T0.timestamp() * 1000) + 100000)]}

    hl = {"0xhl": FakeHL([seed_lead(amount="300")])}
    st = flow.run_chains(hl, ads, {"ethereum": 2, "tron": 2}, {"ethereum": 30, "tron": 30}, resolve, log=lambda _: None)
    assert list(st.chains) == ["ethereum", "tron"]
    tr = st.chains["tron"].accounts
    assert set(tr) == {TRON_R, TRON_NEXT} and tr[TRON_R].parents == {SEED}
    assert engine.LEVEL_NAME[tr[TRON_R].level] == "확정"              # 0x 붙은 LayerZero 도착 tx와 TronScan tx 매칭
    assert sorted(set(eth_calls)) == [SEED] and len(tron_calls) == 2  # 같은 주소를 두 번 조회하지 않는다


def test_stage_leads_are_not_reseeded():
    """홉 상한에 걸려 리드로 남은 송금을 다음 라운드에서 다시 시작점으로 잡지 않는다."""
    eth_raws = {SEED: raw(SEED, internal=[unit_in(SEED)], txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 158, 60, 200)])}
    fetch, calls = counting(eth_raws)
    hl = {"0xhl": FakeHL([seed_lead()])}
    st = flow.run_chains(hl, {"ethereum": adapters.adapter_for("ethereum", fetch)}, {"ethereum": 0}, {"ethereum": 30},
                         log=lambda _: None)
    assert calls == [SEED] and NEXT not in st.chains["ethereum"].accounts


def test_arbitrum_withdrawal_matched_by_amount_minus_fee():
    """HL → Arbitrum USDC 출금 리드의 tx는 HL 해시라 금액(수수료 1 USDC 뺀 값)·시각으로 찾는다."""
    user = "0x119636ce6a13e3d54af8ea3244a285f2efd270cb"
    arb_raw = raw(user, tokens=[tok("0xab", A(HL_BRIDGE), A(user), ARB_USDC, 11353.76, 6, 3, 50, symbol="USDC"),
                                tok("0xac", A(user), A(BINANCE, tags=["Binance 14"]), ARB_USDC, 11353, 6, 30, 60, symbol="USDC")])
    arb_raw["chain"] = "arbitrum"
    lead = Lead("arbitrum", user, "0xhlhash", "USDC", Decimal("11354.76"), None, int(T0.timestamp() * 1000), "0xhl",
                "Arbitrum USDC 출금", 2)
    ad = adapters.adapter_for("arbitrum", lambda a: arb_raw)
    accounts, edges, _ = engine.stage(engine.seeds_from_leads([lead], ad), ad, log=lambda _: None)
    acct = accounts[user]
    assert acct.traced_total == {"USD": Decimal("11353.76")}
    assert edges[0]["result"] == "거래소"


def test_evm_whitelist_is_per_chain():
    """같은 토큰 주소라도 그 체인 화이트리스트에 있어야 인정한다 (Ethereum USDC 주소는 Arbitrum에서 무시)."""
    r = raw(SEED, tokens=[tok("0x1", A(NEXT), A(SEED), ETH_USDC, 500, 6, 1, 10, symbol="USDC"),
                          tok("0x2", A(NEXT), A(SEED), ARB_USDC, 700, 6, 2, 11, symbol="USDC")])
    r["chain"] = "arbitrum"
    ts = adapters.evm_transfers(r)
    assert [t.amount for t in ts] == [Decimal(700)] and ts[0].group == "USD"


def test_start_from_any_chain_address():
    """--start ethereum:<주소>: 들어온 자금 전체를 추적 대상으로 보고 유출을 따라간다."""
    eth_raws = {SEED: raw(SEED, txs=[tx("0x" + "a" * 64, A("0x9999999999999999999999999999999999999999"), A(SEED), 50, 0, 100),
                                     tx("0x" + "b" * 64, A("0x8888888888888888888888888888888888888888"), A(SEED), 30, 10, 110),
                                     tx("0x" + "1" * 64, A(SEED), A(BINANCE, tags=["Binance 14"]), 79, 60, 200)])}
    fetch, _ = counting(eth_raws)
    ad = adapters.adapter_for("ethereum", fetch)
    st = flow.run_chains({}, {"ethereum": ad}, {"ethereum": 2}, {"ethereum": 30},
                         origin={"ethereum": engine.origin_seeds([SEED.upper().replace("0X", "0x")], ad)}, log=lambda _: None)
    acct = st.chains["ethereum"].accounts[SEED]
    assert acct.origin and engine.LEVEL_NAME[acct.level] == "확정"
    assert acct.traced_total == {"ETH": Decimal(80)}
    (lead,) = acct.leads
    assert lead.kind.startswith("거래소 입금")
