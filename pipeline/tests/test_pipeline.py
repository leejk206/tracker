"""파이프라인 오프라인 검증 — 저장된 BTC 추적 결과 + hl_ledger fixture로 단계 연결을 재현한다.

저장소 루트에서: python3 -m pytest pipeline/tests -q
"""
import csv
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline import ROOT, core, report

TRACES = ROOT / "hyperliquid-tracer" / "output" / "traces"
FX = ROOT / "hl_ledger" / "tests" / "fixtures"
CASE2_TRACE = TRACES / "bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37.json"
CASE2_HL = "0x3a37880ff9ebd7b45376ef15bda69cefc0016575"
CASE1_HL = "0xef3fbfa68ac8d56031dab12f48acf9fe07b4c261"
FIRST_XMR1_RECIPIENT = "0x68c4357f7d31ccad23341d414494f33a7dbb65c3"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def recipient_raw():
    """사례 2 첫 XMR1 수신 계정을 흉내 낸 원자료: 추적 자금 1건 수신 → Arbitrum USDC 출금."""
    return {
        "user": FIRST_XMR1_RECIPIENT, "fetchedAt": 0, "fills": [], "balances": [], "spotPairs": {},
        "unit": {"operations": []},
        "ledger": [
            {"time": 1787804561417, "hash": "0xaa", "delta": {
                "type": "send", "user": CASE2_HL, "destination": FIRST_XMR1_RECIPIENT, "token": "XMR1",
                "amount": "92.608539", "usdcValue": "40932.974238", "nonce": 1787804561417}},
            {"time": 1787900000000, "hash": "0xbb", "delta": {"type": "withdraw", "usdc": "40000.0", "nonce": 1}},
        ],
    }


def fetcher(table):
    def fetch(addr, probe=False):
        return table[addr.lower()]()
    return fetch


FIXTURES = {
    CASE2_HL: lambda: load(FX / "case2_raw.json"),
    CASE1_HL: lambda: load(FX / "case1_inflow_raw.json"),
    FIRST_XMR1_RECIPIENT: recipient_raw,
}


def test_seeds_from_saved_trace():
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    assert list(seeds) == [CASE2_HL]
    assert len(seeds[CASE2_HL]["matches"]) == 5
    assert core.LEVEL_NAME[seeds[CASE2_HL]["level"]] == "추정"   # 경로에 다중 입력 tx


def test_case2_btc_to_hl_without_hops():
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, edges, failed = core.hl_stage(seeds, fetcher(FIXTURES), hops=0, log=lambda _: None)
    acct = accounts[CASE2_HL]
    assert not failed and len(accounts) == 1
    assert core.LEVEL_NAME[acct.level] == "계정 단위 확정"
    assert core.LEVEL_NAME[acct.path_level] == "추정"            # BTC 구간이 더 약하다
    assert any("5/18건" in b for b in acct.basis)
    assert len(edges) == 23
    assert [l.chain for l in acct.leads] == ["hyperliquid"] * 23 and all(l.kind == "미추적 HL 송금" for l in acct.leads)


def test_unit_withdrawal_becomes_ethereum_lead():
    accounts, _, _ = core.hl_stage({CASE1_HL: None}, fetcher(FIXTURES), hops=1, log=lambda _: None)
    (lead,) = [l for l in accounts[CASE1_HL].leads if l.kind == "Unit 출금"]
    assert (lead.chain, lead.address) == ("ethereum", "0x49D52e55FA049878446712C4D7E59fc07106DD19")
    assert lead.tx == "0x9dba32b80d62cef7148e959f0f8380f3c83668dc26990ce95c39af91bfd3ea81"
    assert lead.token == "UETH" and core.LEVEL_NAME[lead.level] == "확정"


def test_hl_send_hop_is_followed_until_account_cap():
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, edges, _ = core.hl_stage(seeds, fetcher(FIXTURES), hops=1, max_accounts=2, log=lambda _: None)
    hop = accounts[FIRST_XMR1_RECIPIENT]
    assert hop.hop == 1 and hop.parents == {CASE2_HL}
    assert core.LEVEL_NAME[hop.level] == "확정"                  # 추적 자금 1건이 유일한 유입
    assert core.LEVEL_NAME[hop.path_level] == "추정"             # 경로 확실도는 부모를 넘지 못한다
    (arb,) = hop.leads
    assert (arb.chain, arb.address, arb.kind) == ("arbitrum", FIRST_XMR1_RECIPIENT, "Arbitrum USDC 출금")
    # 상한에 걸린 나머지 22개 수신 계정은 리드로 남는다
    assert len(accounts[CASE2_HL].leads) == 22


def test_hop_with_outside_money_is_estimated():
    def mixed():
        raw = recipient_raw()
        raw["ledger"].insert(1, {"time": 1787850000000, "hash": "0xcc", "delta": {"type": "deposit", "usdc": "5000.0"}})
        return raw
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, _, _ = core.hl_stage(seeds, fetcher({**FIXTURES, FIRST_XMR1_RECIPIENT: mixed}), hops=1,
                                   max_accounts=2, log=lambda _: None)
    assert core.LEVEL_NAME[accounts[FIRST_XMR1_RECIPIENT].level] == "추정"   # 외부 유입 12%


def test_hub_stops_tracing():
    """추적 자금 비중이 1% 미만인 대형 계정(브리지·서비스 지갑)은 허브로 보고 그 유출을 리드로 쏟지 않는다."""
    def hub():
        raw = recipient_raw()
        raw["ledger"] += [{"time": 1787900000000 + i, "hash": f"0xin{i}", "delta": {
            "type": "send", "user": f"0x{i:040x}", "destination": FIRST_XMR1_RECIPIENT, "token": "XMR1",
            "amount": "1000", "usdcValue": "400000", "nonce": i}} for i in range(60)]
        raw["ledger"] += [{"time": 1788000000000 + i, "hash": f"0xout{i}", "delta": {
            "type": "send", "user": FIRST_XMR1_RECIPIENT, "destination": f"0x{i + 100:040x}", "token": "XMR1",
            "amount": "1", "usdcValue": "400", "nonce": 1000 + i}} for i in range(60)]
        return raw
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, edges, _ = core.hl_stage(seeds, fetcher({**FIXTURES, FIRST_XMR1_RECIPIENT: hub}), hops=2,
                                       max_accounts=2, log=lambda _: None)
    acct = accounts[FIRST_XMR1_RECIPIENT]
    assert acct.hub and not acct.sends
    assert all(e["from"].lower() != FIRST_XMR1_RECIPIENT for e in edges)
    (lead,) = acct.leads
    assert lead.kind == "허브 도달 (추적 종료)" and lead.token == "XMR1"


def test_large_account_is_hub_without_full_fetch():
    """hop 1 이상에서 원장 첫 페이지가 꽉 찬 계정은 전체 수집 없이 허브로 멈춘다."""
    def truncated():
        raw = recipient_raw()
        raw["truncated"] = True
        return raw
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, _, _ = core.hl_stage(seeds, fetcher({**FIXTURES, FIRST_XMR1_RECIPIENT: truncated}), hops=2,
                                   max_accounts=2, log=lambda _: None)
    acct = accounts[FIRST_XMR1_RECIPIENT]
    assert acct.hub and any("대형 계정" in b for b in acct.basis)
    (lead,) = acct.leads
    assert lead.amount == Decimal("92.608539") and lead.token == "XMR1"   # 부모 → 허브 송금 간선 기준


def test_hyperevm_bridge_is_lead_not_hop():
    """0x2000…00XX / 0x2222…2222 는 HyperEVM 브리지 시스템 주소 — 계정으로 따라가지 않는다."""
    def to_evm():
        raw = recipient_raw()
        raw["ledger"][1] = {"time": 1787900000000, "hash": "0xdd", "delta": {
            "type": "spotTransfer", "user": FIRST_XMR1_RECIPIENT, "destination": "0x2000000000000000000000000000000000000123",
            "token": "XMR1", "amount": "92.0", "usdcValue": "40000", "nonce": 2}}
        return raw
    seeds = core.seeds_from_trace(load(CASE2_TRACE))
    accounts, _, _ = core.hl_stage(seeds, fetcher({**FIXTURES, FIRST_XMR1_RECIPIENT: to_evm}), hops=3,
                                   max_accounts=2, log=lambda _: None)
    assert "0x2000000000000000000000000000000000000123" not in accounts
    (lead,) = accounts[FIRST_XMR1_RECIPIENT].leads
    assert (lead.chain, lead.address, lead.kind) == ("hyperevm", FIRST_XMR1_RECIPIENT, "HyperEVM 전송")
    assert core.is_hyperevm_bridge("0x" + "2" * 40) and not core.is_hyperevm_bridge(CASE2_HL)


def test_report_files(tmp_path):
    trace = load(CASE2_TRACE)
    accounts, edges, failed = core.hl_stage(core.seeds_from_trace(trace), fetcher(FIXTURES), hops=1,
                                            max_accounts=2, log=lambda _: None)
    core.write_accounts(accounts, tmp_path)
    md = report.write(tmp_path, trace["start"], trace, accounts, edges, failed, {"max_depth": 7, "hl_hops": 1, "max_accounts": 2})
    text = md.read_text(encoding="utf-8")
    assert "```mermaid" in text and "## 5. 다음 체인 리드" in text
    with open(tmp_path / "leads.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 23 and {r["chain"] for r in rows} == {"hyperliquid", "arbitrum"}
    data = load(tmp_path / "pipeline.json")
    assert {a["address"].lower() for a in data["accounts"]} == {CASE2_HL, FIRST_XMR1_RECIPIENT}
    assert (tmp_path / "hl" / CASE2_HL / "report.md").is_file()


def test_reuse_reads_saved_raw(tmp_path):
    saved = tmp_path / "hl" / CASE2_HL
    saved.mkdir(parents=True)
    (saved / "raw.json").write_text((FX / "case2_raw.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert core.raw_fetcher(tmp_path, reuse=True)(CASE2_HL)["user"].lower() == CASE2_HL


@pytest.mark.parametrize("argv", [["--tx", "zz"], ["--hl", "0x123"], ["--hl-hops", "-1", "--hl", "0x" + "a" * 40]])
def test_cli_rejects_bad_input(argv):
    from pipeline.__main__ import main
    with pytest.raises(SystemExit):
        main(argv)
