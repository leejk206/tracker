"""Tron 단계 오프라인 검증 — TronScan token_trc20/transfers 응답 형식의 합성 원자료.

사례 1에서 본 형태: 브리지 도착 주소 → 큰 금액 송금 + 건마다 1.5 USDT 수수료 대납 → 전송 수만 건 허브로 집결.
"""
from decimal import Decimal

from pipeline import tron
from pipeline.core import Lead

USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
R1, R2 = "TMkyyM9RomoGLqQkoNfde2WLvcWi4W1aJk", "TKAs5uZwp6BeN2481nzmHsNNJ5KwffNrcd"
HUB = "TGFYfGHdWVTZovLzRgYEaU9xSQ4gzizDsp"
FEE = "TLntW9Z59LYY5KEi9cmwk3PKjQga828ird"
OTHER = "TOther1111111111111111111111111111"
T0 = 1789300000000


def tr(tx, frm, to, usdt, t):
    return {"transaction_id": tx, "from_address": frm, "to_address": to, "quant": str(int(Decimal(str(usdt)) * 10 ** 6)),
            "block_ts": T0 + t * 1000, "contract_address": USDT, "contractRet": "SUCCESS"}


def raw(addr, rows, total=None, tags=None):
    return {"address": addr, "fetchedAt": 0, "transfers": rows, "tags": tags or {},
            "total": total if total is not None else len(rows), "truncated": (total or 0) > tron.PAGE * tron.MAX_PAGES}


def lead(addr, tx, amount, level=2):
    return Lead("tron", addr, tx, "USDT", Decimal(amount), None, T0, "0xeth", "USDT0 (LayerZero) 도착 (SUCCEEDED)", level)


def run(raws, leads, hops=2):
    return tron.tron_stage(tron.seeds_from_leads(leads), lambda a: raws[a], hops, 30, log=lambda _: None)


def test_two_receivers_converge_on_hub():
    raws = {
        R1: raw(R1, [tr("0xaa", "Tbridge", R1, 745000, 0), tr("b1", R1, HUB, 744000, 100), tr("b2", R1, FEE, 1.5, 100)]),
        R2: raw(R2, [tr("cc", "Tbridge", R2, 996501, 0), tr("d1", R2, HUB, 996000, 200), tr("d2", R2, FEE, 1.5, 200)]),
        # 허브: 전송이 페이지 상한을 넘고, 받아온 최근 기록에 추적 입금이 없다
        HUB: raw(HUB, [tr(f"n{i}", OTHER, HUB, 10, 900 + i) for i in range(5)], total=50000),
    }
    # LayerZero 도착 tx는 0x가 붙어 오고 TronScan tx id에는 없다 → 둘 다 맞춰야 한다
    accounts, edges, failed = run(raws, [lead(R1, "0xAA", "745000"), lead(R2, "0xcc", "996501")])
    assert not failed
    assert tron.LEVEL_NAME[accounts[R1].level] == "확정" and accounts[R1].path_level == 2
    hub = accounts[HUB]
    assert hub.stop == "허브" and hub.parents == {R1, R2}
    (l,) = hub.leads
    assert l.kind.startswith("허브 도달") and l.amount == Decimal(744000 + 996000)
    assert all(e["to"] != FEE for e in edges)            # 1.5 USDT 수수료 대납은 따라가지 않는다


def test_outside_money_makes_estimated_and_expect_match():
    raws = {R1: raw(R1, [tr("zz", "Tbridge", R1, 1000, 0), tr("x", OTHER, R1, 500, 10), tr("y", R1, OTHER, 1500, 20)])}
    accounts, _, _ = run(raws, [lead(R1, "", "1000.2")], hops=0)
    acct = accounts[R1]
    assert any("금액·시각으로 찾음" in b for b in acct.basis)
    assert tron.LEVEL_NAME[acct.level] == "추정"
    (l,) = acct.leads
    assert l.kind.startswith("미추적")


def test_mixed_address_stops():
    """추적 자금보다 남의 자금이 더 많은 주소는 유출을 따라가지 않고 그 주소 하나를 리드로 남긴다."""
    raws = {
        R1: raw(R1, [tr("aa", "Tbridge", R1, 1000, 0), tr("b", R1, HUB, 999, 10)]),
        HUB: raw(HUB, [tr("b", R1, HUB, 999, 10), tr("m", OTHER, HUB, 50000, 20),
                       *[tr(f"o{i}", HUB, f"Tdst{i}", 5000, 30 + i) for i in range(10)]]),
    }
    accounts, edges, _ = run(raws, [lead(R1, "aa", "1000")])
    hub = accounts[HUB]
    assert hub.stop == "혼합 주소" and all(e["from"] != HUB for e in edges)
    (l,) = hub.leads
    assert l.kind.startswith("혼합 주소 도달") and l.amount == Decimal(999)


def test_unmoved_receiver_keeps_balance_lead():
    """도착 후 아직 옮기지 않은 USDT가 결과에서 사라지면 안 된다 (리뷰 지적 1)."""
    raws = {R1: raw(R1, [tr("aa", "Tbridge", R1, 1000000, 0)])}
    accounts, _, _ = run(raws, [lead(R1, "aa", "1000000")])
    (l,) = accounts[R1].leads
    assert l.kind.startswith("잔액 보유") and l.amount == Decimal(1000000)


def test_unmatched_arrival_falls_back_to_all_outflows():
    """도착 tx·금액이 안 맞아도 유출은 후보로 남긴다 (리뷰 지적 1)."""
    raws = {R1: raw(R1, [tr("zz", "Tbridge", R1, 900, 0), tr("y", R1, OTHER, 900, 20)])}
    accounts, edges, _ = run(raws, [lead(R1, "not-here", "1000")], hops=0)
    assert accounts[R1].level == 0 and len(edges) == 1 and accounts[R1].leads[0].address == OTHER


def test_hub_receiver_lead_uses_bridge_amount():
    """허브라 추적 입금을 못 찾은 hop 0 주소는 브리지 도착 금액으로 리드를 남긴다 (리뷰 지적 2)."""
    raws = {R1: raw(R1, [tr(f"n{i}", OTHER, R1, 10, 900 + i) for i in range(5)], total=50000)}
    accounts, _, _ = run(raws, [lead(R1, "aa", "996501")])
    (l,) = accounts[R1].leads
    assert accounts[R1].stop == "허브" and l.amount == Decimal(996501) and l.time == T0


def test_late_branch_merge_is_rejudged():
    """다른 갈래의 추적 자금이 이미 판정한 주소로 나중에 들어오면 다시 판정 (리뷰 지적 3)."""
    B, C = "TBbbb", "TCccc"
    raws = {
        R1: raw(R1, [tr("aa", "Tbridge", R1, 1000, 0), tr("b", R1, B, 500, 10), tr("c", R1, C, 500, 11)]),
        B: raw(B, [tr("b", R1, B, 500, 10), tr("cb", C, B, 500, 30)]),
        C: raw(C, [tr("c", R1, C, 500, 11), tr("cb", C, B, 500, 30)]),
    }
    accounts, _, _ = run(raws, [lead(R1, "aa", "1000")])
    b = accounts[B]
    assert b.parents == {R1, C} and tron.LEVEL_NAME[b.level] == "계정 단위 확정"


def test_row_tag_marks_exchange():
    """태그는 contractInfo가 아니라 각 행의 to_address_tag에 있다 (리뷰 지적 4)."""
    def get(path, params):
        row = {**tr("b", R1, HUB, 999, 10), "to_address_tag": {"to_address_tag": "Binance-Hot 3", "to_address_tag_logo": ""}}
        return {"total": 1, "token_transfers": [row]}
    r = tron.fetch_address(R1, get)
    assert r["tags"] == {HUB: "Binance-Hot 3"}


def test_tx_match_and_amount_match_together():
    """같은 주소에 도착 tx 있는 것과 없는 것이 섞여도 둘 다 추적 입금 (리뷰 지적 6)."""
    raws = {R1: raw(R1, [tr("aa", "Tbridge", R1, 1000, 0), tr("zz", "Tbridge2", R1, 700, 50), tr("y", R1, OTHER, 1700, 99)])}
    accounts, _, _ = run(raws, [lead(R1, "aa", "1000"), lead(R1, "", "700.1")], hops=0)
    acct = accounts[R1]
    assert acct.traced_total == Decimal(1700) and tron.LEVEL_NAME[acct.level] == "계정 단위 확정"


def test_exchange_tag_stops():
    raws = {R1: raw(R1, [tr("aa", "Tbridge", R1, 1000, 0), tr("b", R1, HUB, 999, 10)], tags={HUB: "Binance-Hot 3"})}
    accounts, edges, _ = run(raws, [lead(R1, "aa", "1000")])
    assert edges[0]["result"] == "거래소" and accounts[R1].leads[0].kind == "거래소 입금 (Binance-Hot 3)"


def test_fetch_stops_at_page_cap():
    calls = []

    def get(path, params):
        calls.append(params["start"])
        return {"total": 50000, "token_transfers": [tr(str(i), "a", HUB, 1, i) for i in range(tron.PAGE)],
                "contractInfo": {HUB: {"publicTag": "", "tag1": ""}}}

    r = tron.fetch_address(HUB, get)
    assert len(calls) == tron.MAX_PAGES and r["truncated"] and r["total"] == 50000
