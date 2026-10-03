"""정답 검증 — 수기 사례 정리(기준 2026-10-01)의 수치를 오프라인 원자료로 재현한다.

fixtures/*.json 은 2026-10-02 api.fetch_all() 결과를 그대로 저장한 것.
"""
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from hl_ledger.analyze import analyze, normalize
from hl_ledger.report import render_markdown, render_summary

FX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def case2():
    return analyze(load("case2_raw.json"))


# ── 사례 2: THORChain 해킹 자금, HL 계정 1개에 18회 입금 ──────────────────────

def test_case2_deposits_match_unit(case2):
    dep = [e for e in case2["events"] if e.unit and e.direction == "in"]
    assert len(dep) == 18
    assert all(e.unit["state"] == "done" for e in dep)
    # BTC 쪽 합계(수기 정리 8.1399 BTC) — Unit sourceAmount 기준
    btc = sum(Decimal(e.unit["sourceAmount"]) for e in dep) / Decimal(10**8)
    assert round(btc, 4) == Decimal("8.1399")
    # 수기 정리 표의 첫 입금 tx
    assert dep[0].unit["sourceTxHash"].startswith("685ea58a17e8fa27fe99acc59e41dd4b011024dc6304f12c68a9785734256a2f")
    assert dep[0].hash == "0x1365b473d1bafae514df04430f394d02016a00596cbe19b7b72e5fc690bed4cf"
    assert case2["unmatched_unit"] == []


def test_case2_fills(case2):
    assert len(case2["raw"]["fills"]) == 988
    g = {(x.pair, x.side): x for x in case2["fills"]}
    assert g[("XMR1/USDC", "매수")].size == Decimal("1302.74")
    # 매도 + 먼지 전환 = 입금 전량
    sold = g[("UBTC/USDC", "매도")].size + g[("UBTC/USDC", "매도 (먼지 전환)")].size
    dep = sum(e.amount for e in case2["events"] if e.unit and e.direction == "in")
    assert sold == dep


def test_case2_sends(case2):
    sends = [e for e in case2["events"] if e.direction == "out"]
    assert len(sends) == 23
    assert all(e.kind == "send" and e.token == "XMR1" and not e.unit for e in sends)
    assert round(sum(e.amount for e in sends), 2) == Decimal("1302.08")
    assert len({e.counterparty for e in sends}) == 23
    assert sends[0].hash == "0x6dea1b6a1b55c57a6f630443108b26020217004fb658e44c11b2c6bcda599f65"


def test_case2_verdict(case2):
    v = case2["verdict"]
    assert v.level == "계정 단위 확정"
    assert "기록 누락 의심" not in v.tags


def test_case2_markdown_has_stage_table(case2):
    md = render_markdown(case2)
    assert "| 단계 | 해당 tx 해시 또는 HL 기록 | 연결고리가 되는 식별자 | 연결 확실도 및 근거 | 사용한 조회 경로 |" in md
    assert "## 입금 (18건)" in md and "## 출금 · 송금 (23건)" in md


# ── 사례 1: Lazarus 계정 79개 중 특이 계정 ────────────────────────────────────

def test_case1_rebuy_account():
    res = analyze(load("case1_rebuy_raw.json"))
    assert res["raw"]["user"] == "0x68F79eC571420e307d09A52D138A3004DD509674"
    assert res["verdict"].level == "확정"
    assert "입금 토큰 재매수" in res["verdict"].tags
    out = [e for e in res["events"] if e.unit and e.direction == "out"]
    assert len(out) == 1 and out[0].unit["destinationChain"] in ("ethereum", "solana")


def test_case1_small_inflow_account():
    res = analyze(load("case1_inflow_raw.json"))
    v = res["verdict"]
    assert v.level == "확정" and "소액 외부 유입" in v.tags
    other = [e for e in res["events"] if e.direction == "in" and not e.unit]
    assert len(other) == 2 and all(e.token == "USDC" for e in other)


def test_summary_counts():
    rs = [analyze(load(n)) for n in ("case1_rebuy_raw.json", "case1_inflow_raw.json", "case2_raw.json")]
    md = render_summary(rs)
    assert "확정 2개" in md and "계정 단위 확정 1개" in md


# ── 판정 규칙 (합성 데이터) ──────────────────────────────────────────────────

def test_mixing_downgrades_to_estimate():
    raw = load("case1_inflow_raw.json")
    raw = copy.deepcopy(raw)
    # Arbitrum에서 큰 USDC 입금이 섞이면 1:1이 깨진다
    raw["ledger"].append({"time": raw["ledger"][0]["time"] + 1, "hash": "0xdead",
                          "delta": {"type": "deposit", "usdc": "100000.0"}})
    assert analyze(raw)["verdict"].level == "추정"


def test_truncated_fills_flagged():
    raw = copy.deepcopy(load("case2_raw.json"))
    raw["fills"] = raw["fills"][len(raw["fills"]) // 2:]  # 앞쪽 체결이 잘린 상황
    v = analyze(raw)["verdict"]
    assert "기록 누락 의심" in v.tags


def test_no_unit_deposit():
    raw = copy.deepcopy(load("case2_raw.json"))
    raw["unit"] = {"operations": []}
    assert analyze(raw)["verdict"].level == "해당 없음"


@pytest.mark.parametrize("delta,expected", [
    ({"type": "deposit", "usdc": "5"}, ("in", "USDC")),
    ({"type": "withdraw", "usdc": "5", "nonce": 1, "fee": "1"}, ("out", "USDC")),
    ({"type": "accountClassTransfer", "usdc": "5", "toPerp": True}, ("internal", "USDC")),
    ({"type": "send", "user": "0xme", "destination": "0xme", "token": "UBTC", "amount": "1",
      "sourceDex": "spot", "destinationDex": ""}, ("internal", "UBTC")),
    ({"type": "spotTransfer", "user": "0xother", "destination": "0xme", "token": "UETH", "amount": "1"},
     ("in", "UETH")),
])
def test_normalize(delta, expected):
    direction, token, *_ = normalize("0xME", delta)
    assert (direction, token) == expected
