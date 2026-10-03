"""ETH 단계 오프라인 검증 — 실데이터에서 본 패턴(Unit internal 출금, 주소 오염, 가짜 ETH 토큰, CoW 스왑 → USDT0 브리지)을
Blockscout v2 응답 형식의 합성 원자료로 재현한다.

저장소 루트에서: python3 -m pytest pipeline/tests -q
"""
from datetime import datetime, timezone
from decimal import Decimal

from pipeline import eth
from pipeline.core import LEVEL, Lead

UNIT_TREASURY = "0xbea9f7fd27f4ee20066f18def0bc586ec221055a"
UNIT_BATCH = "0x4bbe9b84aac9804557e8a90b7186324f20357e5c"
SEED = "0x1111111111111111111111111111111111111111"
NEXT = "0x2222000000000000000000000000000000003333"
POISON = "0x2222abcdefabcdefabcdefabcdefabcdef003333"   # NEXT와 앞뒤 4자리가 같다
COW_ETHFLOW = "0xba3cb449bd2b4adddbc894d8697f5170800eadec"
COW_SETTLEMENT = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
USDT0_OFT = "0x1f748c76de468e9d11bd340fa9d5cbadf315dfb0"
BINANCE = "0x28c6c06298d514db089934071355e5743bf21d60"
OTHER = "0x9999999999999999999999999999999999999999"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
FAKE_ETH = "0xf5cff8bdeb6589869a8fa79d5581343f1e04d86c"
T0 = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SEED_TX = "0x" + "a" * 64


def iso(minutes):
    return datetime.fromtimestamp(T0.timestamp() + minutes * 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def A(h, name=None, tags=(), contract=False):
    return {"hash": h, "name": name, "is_contract": contract, "public_tags": [],
            "metadata": {"tags": [{"name": t, "tagType": "name"} for t in tags]} if tags else None}


def tx(h, frm, to, eth_amt, minute, block):
    return {"hash": h, "from": frm, "to": to, "value": str(int(Decimal(str(eth_amt)) * 10 ** 18)),
            "result": "success", "timestamp": iso(minute), "block_number": block}


def itx(h, frm, to, eth_amt, minute, block, index=0):
    return {"transaction_hash": h, "from": frm, "to": to, "value": str(int(Decimal(str(eth_amt)) * 10 ** 18)),
            "success": True, "timestamp": iso(minute), "block_number": block, "index": index}


def tok(h, frm, to, contract, amount, decimals, minute, block, li=0, symbol="USDT"):
    return {"transaction_hash": h, "from": frm, "to": to, "timestamp": iso(minute), "block_number": block,
            "log_index": li, "token": {"address_hash": contract, "symbol": symbol},
            "total": {"value": str(int(Decimal(str(amount)) * 10 ** decimals)), "decimals": str(decimals)}}


def raw(address, txs=(), internal=(), tokens=(), info=None, truncated=False, balance=0):
    return {"address": address, "fetchedAt": 0, "info": {**(info or A(address)), "coin_balance": str(balance)},
            "txs": list(txs), "internal": list(internal), "tokens": list(tokens), "truncated": truncated}


def unit_in(to, amount=158.66, minute=0, block=100, h=SEED_TX):
    """HyperUnit 출금: Treasury가 배치 컨트랙트를 호출하고 internal tx로 받는 쪽에 ETH가 간다 (실데이터 형태)."""
    return itx(h, A(UNIT_BATCH, contract=True), A(to), amount, minute, block)


def seed_lead(address=SEED, tx_hash=SEED_TX, amount="158.66", level=3):
    return Lead("ethereum", address, tx_hash, "UETH", Decimal(amount), None, int(T0.timestamp() * 1000),
                "0xhl", "Unit 출금", level)


def run(raws, leads, hops=2, max_addresses=30):
    seeds = eth.seeds_from_leads(leads)
    return eth.eth_stage(seeds, lambda a: raws[a.lower()], hops, max_addresses, log=lambda _: None)


def poison_spam(to, n=5):
    """먼지 internal + 가짜 ETH 토큰 — 화이트리스트·먼지 기준에 모두 걸러져야 한다."""
    rows = [itx("0x" + f"{i:064x}", A(POISON), A(to), "0.00000001", 30 + i, 120 + i, i) for i in range(n)]
    fake = [tok("0x" + f"{i + 100:064x}", A(to), A(POISON), FAKE_ETH, 158, 18, 40 + i, 130 + i, symbol="ETH")
            for i in range(n)]
    return rows, fake


def test_unit_withdrawal_to_exchange_is_confirmed_and_stops():
    spam, fake = poison_spam(SEED)
    raws = {SEED: raw(SEED, internal=[unit_in(SEED)] + spam, tokens=fake,
                      txs=[tx("0x" + "b" * 64, A(SEED), A(BINANCE, tags=["Binance 14"]), 158.65, 60, 200)])}
    accounts, edges, failed = run(raws, [seed_lead()])
    acct = accounts[SEED]
    assert not failed
    assert eth.LEVEL_NAME[acct.level] == "확정" and eth.LEVEL_NAME[acct.path_level] == "확정"
    assert any("새 주소" in b for b in acct.basis) and any("오염·먼지 유입 5건" in b for b in acct.basis)
    (lead,) = acct.leads
    assert lead.kind.startswith("거래소 입금") and lead.address == BINANCE and lead.amount == Decimal("158.65")
    assert edges[0]["result"] == "거래소"


def test_outside_money_and_prior_balance_make_it_estimated():
    raws = {SEED: raw(SEED, internal=[unit_in(SEED)], txs=[
        tx("0x" + "c" * 64, A(OTHER), A(SEED), 20, -600, 50),          # 추적 입금 전부터 있던 돈
        tx("0x" + "b" * 64, A(SEED), A(BINANCE, tags=["Binance 14"]), 178, 60, 200)])}
    accounts, _, _ = run(raws, [seed_lead()])
    acct = accounts[SEED]
    assert eth.LEVEL_NAME[acct.level] == "추정"
    assert any("이전 거래 1건 (20 ETH)" in b for b in acct.basis)


def test_dex_swap_is_followed_into_bridge():
    """ETH → CoW 스왑 → USDT 수령 → USDT0(OFT) 브리지. 스왑은 종착이 아니라 전환."""
    cow = A(COW_ETHFLOW, "CoWSwapEthFlow", ["CoW Protocol: CoW Swap Eth Flow"], contract=True)
    settle = A(COW_SETTLEMENT, "GPv2Settlement", ["CoW Protocol: GPv2Settlement"], contract=True)
    oft = A(USDT0_OFT, "UsdtOFT", contract=True)
    raws = {SEED: raw(SEED, internal=[unit_in(SEED)],
                      txs=[tx("0x" + "d" * 64, A(SEED), cow, 158.6, 10, 110)],
                      tokens=[tok("0x" + "e" * 64, settle, A(SEED), USDT, 700000, 6, 20, 120),
                              tok("0x" + "f" * 64, A(SEED), oft, USDT, 700000, 6, 30, 130)])}
    accounts, edges, _ = run(raws, [seed_lead()])
    acct = accounts[SEED]
    assert len(acct.swaps_in) == 1 and acct.traced_total["USD"] == Decimal(700000)
    assert eth.LEVEL_NAME[acct.level] == "계정 단위 확정"            # 스왑 수령은 혼입이 아니다
    assert [e["result"] for e in edges] == ["스왑", "브리지"]
    (lead,) = acct.leads
    assert lead.kind.startswith("브리지 입금") and lead.token == "USDT"


def test_follow_next_hop_and_poisoning_victim_warning():
    spam = [itx("0x" + f"{9:064x}", A(POISON), A(SEED), "0.00000001", 5, 105)]
    raws = {
        SEED: raw(SEED, internal=[unit_in(SEED)] + spam,
                  txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 150, 60, 200),
                       tx("0x" + "2" * 64, A(SEED), A(POISON), 8, 61, 201)]),   # 오염 주소로 실제 송금
        NEXT: raw(NEXT, txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 150, 60, 200)]),
        POISON: raw(POISON, txs=[tx("0x" + "2" * 64, A(SEED), A(POISON), 8, 61, 201)]),
    }
    accounts, edges, _ = run(raws, [seed_lead(level=1)], hops=1)
    seed, nxt = accounts[SEED], accounts[NEXT]
    assert any("오염 피해 의심" in b for b in seed.basis)
    assert nxt.hop == 1 and nxt.parents == {SEED}
    assert eth.LEVEL_NAME[nxt.level] == "확정" and eth.LEVEL_NAME[nxt.path_level] == "추정"   # 부모 경로를 넘지 못함
    assert {e["result"] for e in edges} == {"추적"}


def test_returned_funds_are_not_mixing():
    """A → B → A로 되돌아온 추적 자금은 '그 외 유입'이 아니다."""
    raws = {
        SEED: raw(SEED, internal=[unit_in(SEED)],
                  txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 50, 60, 200),
                       tx("0x" + "2" * 64, A(NEXT), A(SEED), 50, 70, 210),
                       tx("0x" + "3" * 64, A(SEED), A(BINANCE, tags=["Binance 14"]), 158, 80, 220)]),
        NEXT: raw(NEXT, txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 50, 60, 200),
                             tx("0x" + "2" * 64, A(NEXT), A(SEED), 50, 70, 210)]),
    }
    # B를 따라가지 않아도 (hops=0) 되돌아온 자금으로 인식
    accounts, _, _ = run(raws, [seed_lead()], hops=0)
    acct = accounts[SEED]
    assert len(acct.returned) == 1 and eth.LEVEL_NAME[acct.level] == "계정 단위 확정"
    # B를 따라가면 B → A 송금이 추적 간선이 되어 다시 판정에서 추적 입금으로 들어간다
    accounts, _, _ = run(raws, [seed_lead()], hops=1)
    acct = accounts[SEED]
    assert eth.LEVEL_NAME[acct.level] == "계정 단위 확정" and not any("그 외 유입" in b for b in acct.basis)


def test_late_branch_merge_is_rejudged():
    """두 갈래가 한 주소로 합류할 때, BFS상 먼저 판정됐더라도 나중 갈래를 추적 자금으로 다시 센다."""
    seed2 = "0x3333333333333333333333333333333333333333"
    mid = "0x4444444444444444444444444444444444444444"
    merge = "0x5555555555555555555555555555555555555555"
    t_mid, t_m1, t_m2 = "0x" + "4" * 64, "0x" + "5" * 64, "0x" + "6" * 64
    raws = {
        SEED: raw(SEED, internal=[unit_in(SEED)], txs=[tx(t_m1, A(SEED), A(merge), 100, 60, 200)]),
        seed2: raw(seed2, internal=[unit_in(seed2, h="0x" + "8" * 64)], txs=[tx(t_mid, A(seed2), A(mid), 100, 60, 200)]),
        merge: raw(merge, txs=[tx(t_m1, A(SEED), A(merge), 100, 60, 200), tx(t_m2, A(mid), A(merge), 100, 90, 300)]),
        mid: raw(mid, txs=[tx(t_mid, A(seed2), A(mid), 100, 60, 200), tx(t_m2, A(mid), A(merge), 100, 90, 300)]),
    }
    accounts, _, _ = run(raws, [seed_lead(), seed_lead(seed2, "0x" + "8" * 64)], hops=2)
    m = accounts[merge]
    assert m.hop == 1 and m.parents == {SEED, mid}
    assert eth.LEVEL_NAME[m.level] == "계정 단위 확정"          # 다시 판정 전에는 '추정'이었다
    assert not any("그 외 유입" in b for b in m.basis)


def test_hop_limit_leaves_lead():
    raws = {SEED: raw(SEED, internal=[unit_in(SEED)], txs=[tx("0x" + "1" * 64, A(SEED), A(NEXT), 150, 60, 200)])}
    accounts, edges, _ = run(raws, [seed_lead()], hops=0)
    (lead,) = accounts[SEED].leads
    assert lead.kind.startswith("미추적") and lead.address == NEXT and edges[0]["result"] == "리드"


def test_lead_without_tx_matched_by_amount_and_time():
    raws = {SEED: raw(SEED, internal=[unit_in(SEED, amount=158.44, minute=30, h="0x" + "7" * 64)])}
    accounts, _, _ = run(raws, [seed_lead(tx_hash="", amount="158.4401")])
    acct = accounts[SEED]
    assert any("금액·시각으로 찾음" in b for b in acct.basis) and acct.traced_total["ETH"] == Decimal("158.44")


def test_truncated_without_traced_inflow_is_hub():
    raws = {SEED: raw(SEED, truncated=True, txs=[tx("0x" + "3" * 64, A(OTHER), A(SEED), 5, 900, 900)])}
    accounts, _, _ = run(raws, [seed_lead()])
    assert accounts[SEED].stop == "허브"


def test_remaining_balance_becomes_lead():
    raws = {SEED: raw(SEED, internal=[unit_in(SEED)], balance=10 ** 20)}   # 100 ETH 남음
    accounts, _, _ = run(raws, [seed_lead()])
    (lead,) = accounts[SEED].leads
    assert lead.kind.startswith("잔액 보유") and lead.amount == Decimal(100)


def test_seeds_group_leads_by_destination():
    seeds = eth.seeds_from_leads([seed_lead(), seed_lead(tx_hash="0x" + "9" * 64, level=1),
                                  Lead("hyperliquid", OTHER, "", "X", Decimal(1), None, 0, "0xhl", "HL", 3)])
    assert list(seeds) == [SEED] and len(seeds[SEED]["txs"]) == 2 and seeds[SEED]["level"] == LEVEL["확정"]
