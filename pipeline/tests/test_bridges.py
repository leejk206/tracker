"""브리지 목적지 해석 오프라인 검증 — 사례 1 실데이터 응답에서 뽑은 값으로 재현한다.

저장소 루트에서: python3 -m pytest pipeline/tests -q
"""
from decimal import Decimal

from pipeline import bridges
from pipeline.core import Lead

DEB_TX = "0x76d754a115d88da51ba0144d8ef48f738109d7dad8274d3edfce76c72535e5b2"
DEB_ORDER = "0x8c21c7cf0ac9c0467a8da820b0b09db74f5efe6c42216d3d8ddef9769cb06965"
LZ_TX = "0xdc8e1eb5f3222aa0059d5fb690c651b2812525a912a7219bff6ae624c9a61292"
# deBridge API가 주는 receiverDst의 바이트와 문자열 표기 (Tron 주소 변환을 독립 검증)
TRON_BYTES = bytes([35, 26, 101, 165, 38, 200, 164, 195, 162, 178, 202, 74, 108, 18, 61, 61, 48, 153, 121, 132])
TRON_STR = "TDApGsYobnq34xJeEHKbDoAbXXbKiqBMSw"


def big(v):
    return {"bigIntegerValue": v, "stringValue": str(v)}


def s(v):
    return {"stringValue": v}


ORDER = {
    "state": "ClaimedUnlock",
    "makerSrc": s("0xae0492dbb1e5b18d0252e3f0ad642ddb0eea52af"),
    "receiverDst": s(TRON_STR),
    "giveOfferWithMetadata": {"chainId": big(1), "amount": big(734251475404), "decimals": 6, "symbol": "USDC"},
    "takeOfferWithMetadata": {"chainId": big(100000026), "amount": big(733498773674), "decimals": 6, "symbol": "USDT"},
    "actualFulfillAmount": big(733498773674),
    "createdSrcEventMetadata": {"blockTimeStamp": 1788071243},
    "fulfilledDstEventMetadata": {"transactionHash": s("9c29acb9d7cfc648b09b17cbe360c804c526368a4a1bdd1231dd0efe10997900"),
                                  "blockTimeStamp": 1788071331},
}
LZ_MSG = {"data": [{
    "pathway": {"srcEid": 30101, "dstEid": 30420, "sender": {"name": "USDT0", "chain": "ethereum"},
                "receiver": {"name": "USDT0", "chain": "tron"}},
    "source": {"tx": {"txHash": LZ_TX, "from": "0xf43e8dc4e3e35beac84daf31acd7bec6c7ff6945", "blockTimestamp": 1788235379,
                      "payload": "0x000200000000000000000000000064ed7f8ae01d129b918127aab553a234e15bec01000000e804247fc7"}},
    "destination": {"status": "SUCCEEDED", "tx": {"txHash": "0xe77f9f2b4372dd5ebd504c43036f6626cd228f7d86691a6ef41f2ce4c03bb214",
                                                  "blockTimestamp": 1788235608}},
}]}


def fake_get(table):
    return lambda url: table.get(url, {})


DLN_TABLE = {
    f"{bridges.DLN}/Transaction/{DEB_TX}/orderIds": {"orderIds": [{"stringValue": DEB_ORDER}]},
    f"{bridges.DLN}/Orders/{DEB_ORDER}": ORDER,
}


def lead(kind, address, tx):
    return Lead("ethereum", address, tx, "ETH", Decimal(300), None, 0, "0xsrc", kind, 2)


def test_tron_address_encoding_matches_debridge():
    assert bridges.chain_address("tron", b"\0" * 12 + TRON_BYTES) == TRON_STR


def test_debridge_order_to_tron():
    (h,) = bridges.resolve_debridge(DEB_TX, fake_get(DLN_TABLE))
    assert (h.dst_chain, h.receiver, h.token, h.amount) == ("tron", TRON_STR, "USDT", Decimal("733498.773674"))
    assert h.status == "ClaimedUnlock" and h.dst_tx.startswith("9c29acb9")


def test_cancelled_debridge_order_is_refund():
    cancelled = {**ORDER, "state": "ClaimedOrderCancel", "fulfilledDstEventMetadata": None,
                 "claimedOrderCancelSrcEventInfo": {"transactionMetadata": {"transactionHash": s("0xrefund"),
                                                                             "blockTimeStamp": 1788071500}}}
    (h,) = bridges.resolve_debridge(DEB_TX, fake_get({**DLN_TABLE, f"{bridges.DLN}/Orders/{DEB_ORDER}": cancelled}))
    assert h.protocol == "deBridge 취소·환불" and h.dst_chain == "ethereum"
    assert h.receiver == "0xae0492dbb1e5b18d0252e3f0ad642ddb0eea52af" and h.token == "USDC"


def test_layerzero_oft_payload_to_tron():
    (h,) = bridges.resolve_layerzero(LZ_TX, fake_get({f"{bridges.LZ}/messages/tx/{LZ_TX}": LZ_MSG}))
    assert (h.dst_chain, h.receiver) == ("tron", "TKAs5uZwp6BeN2481nzmHsNNJ5KwffNrcd")
    assert h.amount == Decimal("996501.913543") and h.status == "SUCCEEDED"


def test_resolve_picks_protocol_and_arrival_replaces_lead():
    table = {**DLN_TABLE, f"{bridges.LZ}/messages/tx/{LZ_TX}": LZ_MSG}
    deb = lead("브리지 입금 (deBridge: Crosschain Forwarder)", "0x663dc15d3c1ac63ff12e45ab68fea3f0a883c251", DEB_TX)
    oft = lead("브리지 입금 (UsdtOFT)", "0x1f748c76de468e9d11bd340fa9d5cbadf315dfb0", LZ_TX)
    other = lead("거래소 입금 (Binance 14)", "0x28c6c06298d514db089934071355e5743bf21d60", "0x" + "1" * 64)
    hops = bridges.resolve_all([deb, oft, other], fake_get(table), log=lambda _: None)
    assert set(hops) == {DEB_TX, LZ_TX}
    out = bridges.arrival_leads([deb, oft, other], hops)
    assert sorted(l.chain for l in out) == ["ethereum", "tron", "tron"]
    assert all(l.level == 2 and l.source == "0xsrc" for l in out)
