"""파이프라인 단계: BTC 추적 → HL 계정 분석 → HL 송금 홉 추적 → 다음 체인 리드.

네트워크는 btc_trace()와 fetch 함수에만 있다. 나머지는 저장된 dict만 보고 동작한다.
"""
from __future__ import annotations

import json
import re
import time
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from . import ROOT  # noqa: F401  (sys.path 설정)
from hl_ledger import api as hl_api
from hl_ledger.analyze import MIXING_THRESHOLD, Event, analyze, dec
from hl_ledger.report import chain_tx, write_all

ADDR = re.compile(r"0x[0-9a-fA-F]{40}")

# 연결 확실도 등급. 경로 전체의 확실도는 구간 중 가장 낮은 등급이다.
LEVEL = {"confirmed": 3, "estimated": 1, "unresolved": 0,
         "확정": 3, "계정 단위 확정": 2, "추정": 1, "해당 없음": 0}
LEVEL_NAME = {3: "확정", 2: "계정 단위 확정", 1: "추정", 0: "미확인"}
HL_SEND_KINDS = ("send", "spotTransfer", "subAccountTransfer")
# 송금으로 따라간 계정이 이보다 유입이 많고 추적 자금 비중이 HUB_SHARE 미만이면
# 브리지·거래소·서비스 지갑 같은 허브로 보고 거기서 멈춘다 (남의 자금 수천 건이 리드로 쏟아지는 것 방지).
HUB_MIN_INFLOWS = 50
# HyperCore → HyperEVM 전송용 시스템 주소: HYPE는 0x2222…2222, 그 외 현물 토큰은 0x20…00 + 토큰 인덱스.
# 계정이 아니라 체인 간 브리지라서 따라가지 않고 hyperevm 리드로 남긴다.
HYPE_SYSTEM = "0x" + "2" * 40
SYSTEM_PREFIX = "0x20" + "0" * 22


def is_hyperevm_bridge(addr: str) -> bool:
    a = addr.lower()
    return a == HYPE_SYSTEM or a.startswith(SYSTEM_PREFIX)
HUB_SHARE = Decimal("0.01")


# ── 1단계: BTC → HL 계정 (hyperliquid-tracer) ──────────────────────────────

def btc_trace(tx: str | None = None, address: str | None = None, max_depth: int = 7) -> dict:
    """hyperliquid-tracer의 BTCTracer를 그대로 실행하고 결과 dict(JSON 저장 형식)를 돌려준다."""
    import config  # hyperliquid-tracer/config.py
    from clients.bitcoin import BitcoinClient
    from clients.hyperunit import HyperUnitClient
    from tracing.btc_tracer import BTCTracer

    tracer = BTCTracer(BitcoinClient(config.ESPLORA_BASE_URL), HyperUnitClient(config.HYPERUNIT_BASE_URL))
    result = tracer.trace_btc_tx(tx.lower(), max_depth) if tx else tracer.trace_address(address, max_depth)
    return result.to_dict()


def seeds_from_trace(trace: dict) -> dict[str, dict]:
    """BTC 추적 매칭 → HL 계정별 묶음. 계정의 BTC 구간 확실도는 매칭 중 가장 높은 것."""
    seeds: dict[str, dict] = {}
    for m in trace["matches"]:
        acct = m.get("hyperliquid_account")
        if not acct:
            continue
        s = seeds.setdefault(acct.lower(), {"address": acct, "matches": [], "level": 0})
        s["matches"].append(m)
        s["level"] = max(s["level"], LEVEL.get(m["confidence"], 0))
    return seeds


# ── 2단계: HL 계정 분석 (hl_ledger) + 송금 홉 ─────────────────────────────

@dataclass
class Lead:
    """파이프라인이 더 따라가지 않은 다음 목적지 — 다음 체인 추적의 입력."""
    chain: str
    address: str
    tx: str
    token: str
    amount: Decimal
    usdc_value: Decimal | None
    time: int
    source: str      # 이 자금이 나간 HL 계정
    kind: str        # Unit 출금 / Arbitrum USDC 출금 / 미추적 HL 송금 / HL 볼트 예치
    level: int = 0   # 시작점부터 이 리드까지 경로 확실도


@dataclass
class Account:
    address: str
    hop: int
    res: dict
    parents: set[str] = field(default_factory=set)
    btc: dict | None = None          # hop 0: seeds_from_trace 항목
    level: int = 0                   # 이 계정 구간만의 확실도
    path_level: int = 0              # 시작점 → 이 계정 경로 확실도
    basis: list[str] = field(default_factory=list)
    sends: list[Event] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    hub: bool = False

    @property
    def key(self) -> str:
        return self.address.lower()


def is_hl_send(e: Event) -> bool:
    return e.direction == "out" and not e.unit and e.kind in HL_SEND_KINDS and bool(ADDR.fullmatch(e.counterparty))


def inflow_split(res: dict, parents: set[str]) -> tuple[list[Event], list[Event]]:
    """유입을 추적 자금(부모 계정에서 온 것)과 그 외로 나눈다."""
    ins = [e for e in res["events"] if e.direction == "in"]
    return [e for e in ins if e.counterparty in parents], [e for e in ins if e.counterparty not in parents]


def looks_like_hub(res: dict, parents: set[str]) -> bool:
    traced, other = inflow_split(res, parents)
    if len(traced) + len(other) < HUB_MIN_INFLOWS:
        return False
    traced_val = sum((e.usdc_value or 0 for e in traced), Decimal(0))
    total_val = traced_val + sum((e.usdc_value or 0 for e in other), Decimal(0))
    return bool(total_val) and traced_val / total_val < HUB_SHARE


def judge_seed(acct: Account) -> None:
    """BTC로 찾은 계정: hl_ledger 판정 + BTC 구간 확실도. 추적된 입금이 계정 Unit 입금 중 얼마인지도 적는다."""
    v = acct.res["verdict"]
    acct.level = LEVEL.get(v.level, 0)
    acct.basis.append(f"HL 판정 {v.level}: {v.summary}")
    if acct.btc is None:
        acct.path_level = acct.level
        return
    traced = {m["operation"].get("operationId") for m in acct.btc["matches"] if m.get("operation")}
    unit_in = [e for e in acct.res["events"] if e.unit and e.direction == "in"]
    hit = [e for e in unit_in if e.unit["operationId"] in traced]
    total = sum((e.amount for e in unit_in), Decimal(0))
    got = sum((e.amount for e in hit), Decimal(0))
    share = f"{got / total * 100:.1f}%" if total else "-"
    acct.basis.append(f"BTC 추적이 닿은 Unit 입금 {len(hit)}/{len(unit_in)}건 (입금량의 {share})")
    btc_level = acct.btc["level"]
    acct.basis.append(f"BTC 구간 {LEVEL_NAME[btc_level]} (BTC 추적 매칭 {len(acct.btc['matches'])}건)")
    acct.path_level = min(btc_level, acct.level)


def judge_hop(acct: Account, accounts: dict[str, Account]) -> None:
    """송금으로 따라온 계정: 추적 자금(부모 계정에서 온 송금) 외 유입이 얼마나 섞였는지로 판정."""
    parent_level = max((accounts[p].path_level for p in acct.parents if p in accounts), default=0)
    if acct.res["raw"].get("truncated"):
        acct.level = 1
        acct.basis.append(f"원장 {hl_api.HL_PAGE_LIMIT:,}건 이상 대형 계정 — 허브 의심, 전체 수집 생략하고 추적 종료")
        acct.path_level = min(parent_level, acct.level)
        return
    traced, other = inflow_split(acct.res, acct.parents)
    traced_val = sum((e.usdc_value or 0 for e in traced), Decimal(0))
    other_val = sum((e.usdc_value for e in other if e.usdc_value is not None), Decimal(0))
    unknown = [e for e in other if e.usdc_value is None]
    ratio = other_val / traced_val if traced_val else Decimal(1)
    acct.basis.append(f"추적 자금 유입 {len(traced)}건 ≈ {traced_val:,.2f} USDC, "
                      f"그 외 유입 {len(other)}건 ≈ {other_val:,.2f} USDC ({ratio * 100:.2f}%)")
    if unknown:
        acct.basis.append(f"가치 미상 유입 {len(unknown)}건")
    if ratio >= MIXING_THRESHOLD or unknown:
        acct.level = 1
    else:
        acct.level = 3 if len(traced) == 1 and not other else 2
    acct.basis.append(f"계정 구간 {LEVEL_NAME[acct.level]}")
    if acct.hub:
        acct.basis.append(f"허브 의심 — 유입 {len(traced) + len(other):,}건 중 추적 자금 비중 1% 미만. "
                          "브리지·거래소·서비스 지갑일 가능성이 커서 여기서 추적 종료")
    acct.path_level = min(parent_level, acct.level)


def collect_leads(acct: Account, followed: set[str], incoming: list[dict]) -> None:
    """계정의 유출 중 파이프라인이 더 따라가지 않은 것 = 다음 체인 추적 리드."""
    if acct.hub:
        # 허브의 유출은 남의 자금과 섞여 있으므로 허브 자체를 리드 하나로 남긴다
        tokens = sorted({e["token"] for e in incoming})
        amount = sum((e["amount"] for e in incoming), Decimal(0)) if len(tokens) == 1 else Decimal(0)
        value = sum((e["usdc_value"] or 0 for e in incoming), Decimal(0))
        last = max((e["time"] for e in incoming), default=0)
        acct.leads.append(Lead("hyperliquid", acct.address, "", "/".join(tokens) or "-", amount, value, last,
                               acct.address, "허브 도달 (추적 종료)", acct.path_level))
        return
    for e in acct.res["events"]:
        if e.direction != "out":
            continue
        if e.unit:
            op = e.unit
            lead = Lead(op["destinationChain"], op["destinationAddress"], chain_tx(op, "destination"),
                        e.token, e.amount, e.usdc_value, e.time, acct.address, "Unit 출금")
        elif e.kind == "withdraw":
            # HL USDC 출금은 같은 주소의 Arbitrum으로 나간다. Arbitrum tx는 HL 원장에 없어 HL hash를 남긴다.
            lead = Lead("arbitrum", acct.address, e.hash, "USDC", e.amount, e.usdc_value, e.time,
                        acct.address, "Arbitrum USDC 출금")
        elif is_hl_send(e) and is_hyperevm_bridge(e.counterparty):
            # HyperEVM 쪽 수신 주소는 보낸 계정과 같은 주소다
            lead = Lead("hyperevm", acct.address, e.hash, e.token, e.amount, e.usdc_value, e.time,
                        acct.address, "HyperEVM 전송")
        elif is_hl_send(e):
            if e.counterparty in followed:
                continue
            lead = Lead("hyperliquid", e.counterparty, e.hash, e.token, e.amount, e.usdc_value, e.time,
                        acct.address, "미추적 HL 송금")
        elif e.kind in ("vaultDeposit", "vaultCreate"):
            lead = Lead("hyperliquid-vault", e.counterparty, e.hash, e.token, e.amount, e.usdc_value, e.time,
                        acct.address, "HL 볼트 예치")
        else:
            continue
        lead.level = acct.path_level
        acct.leads.append(lead)


def raw_fetcher(out: Path | None, reuse: bool) -> Callable[..., dict]:
    """HL 원자료 수집기. reuse면 out/hl/<계정>/raw.json이 있을 때 다시 받지 않는다.

    probe면 원장 첫 페이지만 먼저 본다. 페이지가 꽉 차면(대형 계정) 전체 수집 없이 첫 페이지만 돌려준다 —
    수십만 건짜리 허브를 끝까지 받다가 API 한도에 걸려 수십 분 멈추는 것 방지.
    """
    def fetch(addr: str, probe: bool = False) -> dict:
        cached = out / "hl" / addr.lower() / "raw.json" if out else None
        if reuse and cached and cached.is_file():
            return json.loads(cached.read_text(encoding="utf-8"))
        if probe:
            page = hl_api.hl_info({"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": 0})
            if len(page) >= hl_api.HL_PAGE_LIMIT:
                return {"user": addr, "fetchedAt": int(time.time() * 1000), "ledger": page, "fills": [],
                        "balances": [], "unit": {"operations": []}, "spotPairs": {}, "truncated": True}
        return hl_api.fetch_all(addr)
    return fetch


def hl_stage(seeds: dict[str, dict | None], fetch: Callable[[str], dict], hops: int = 1,
             max_accounts: int = 50, log=print) -> tuple[dict[str, Account], list[dict], list[tuple[str, str]]]:
    """seeds(계정 → BTC 매칭 묶음 또는 None)에서 시작해 HL 송금을 hops단계까지 따라간다.

    돌려주는 것: 계정들, 송금 간선, 수집 실패 목록.
    """
    accounts: dict[str, Account] = {}
    edges: list[dict] = []
    failed: list[tuple[str, str]] = []
    queue: list[tuple[str, int, set[str]]] = [(a, 0, set()) for a in seeds]
    pending: dict[str, set[str]] = {}
    skipped: set[str] = set()

    while queue:
        addr, hop, parents = queue.pop(0)
        key = addr.lower()
        if key in accounts:
            accounts[key].parents |= parents
            continue
        if len(accounts) >= max_accounts:
            skipped.add(key)
            continue
        log(f"[HL hop {hop}] {addr} ({len(accounts) + 1}/{max_accounts}) 수집 중…")
        try:
            raw = fetch(addr, probe=hop > 0)
        except hl_api.ApiError as e:
            log(f"  실패: {e}")
            failed.append((addr, str(e)))
            continue
        seed = seeds.get(key) if hop == 0 else None
        # BFS라 이 계정을 꺼낼 때쯤이면 이전 홉에서 이 계정으로 보낸 부모가 pending에 다 모여 있다
        parents = parents | pending.get(key, set())
        acct = Account((seed or {}).get("address", raw["user"]), hop, analyze(raw), parents, seed)
        accounts[key] = acct
        acct.hub = hop > 0 and (raw.get("truncated") or looks_like_hub(acct.res, parents))
        if acct.hub:
            size = f"{len(raw['ledger']):,}건 이상" if raw.get("truncated") else f"{len(raw['ledger']):,}건"
            log(f"  허브 의심 — 추적 종료 (원장 {size})")
            continue
        acct.sends = [e for e in acct.res["events"] if is_hl_send(e)]
        for e in acct.sends:
            edges.append({"from": acct.address, "to": e.counterparty, "token": e.token, "amount": e.amount,
                          "usdc_value": e.usdc_value, "time": e.time, "hash": e.hash, "kind": e.kind})
            if hop < hops and not is_hyperevm_bridge(e.counterparty):
                queue.append((e.counterparty, hop + 1, {key}))
                pending.setdefault(e.counterparty, set()).add(key)

    # 홉 판정은 부모 판정이 끝난 뒤에 (hop 순서대로)
    for acct in sorted(accounts.values(), key=lambda a: a.hop):
        acct.parents |= pending.get(acct.key, set())
        if acct.hop == 0:
            judge_seed(acct)
        else:
            judge_hop(acct, accounts)
    followed = set(accounts)
    for acct in accounts.values():
        collect_leads(acct, followed, [e for e in edges if e["to"].lower() == acct.key])
    if skipped:
        log(f"계정 상한 {max_accounts}개 도달 — {len(skipped)}개 계정은 리드로만 남김 (--max-accounts로 조정)")
    return accounts, edges, failed


def write_accounts(accounts: dict[str, Account], out: Path) -> None:
    for acct in accounts.values():
        write_all(acct.res, out / "hl" / acct.key)


def log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr)
