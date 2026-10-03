"""Tron 구간 추적: 브리지 도착 주소에서 시작해 TRC-20 USDT 흐름을 홉 단위로 따라간다.

데이터: TronScan 공개 API `token_trc20/transfers` (키 없음). 페이지 상한(2,000건)을 넘는 주소는
추적 입금을 못 찾으면 허브로 보고 멈춘다. 키 없이는 주소 태그가 거의 비어 있어 거래소 판별은 태그가 있을 때만 한다.

판정 기준은 ETH 단계와 같다: 추적 자금 외 유입 + 추적 입금 이전 잔액이 1% 미만이면 확정/계정 단위 확정.
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .core import LEVEL_NAME, MIXING_THRESHOLD, Lead

API = "https://apilist.tronscanapi.com/api"
USER_AGENT = "tracker-pipeline/0.1"
TOKENS = {"TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6)}   # Tron USDT (TRC-20)
PAGE = 50
MAX_PAGES = 40             # 주소당 최대 2,000건. 넘으면 truncated (오염 스팸이 많은 주소도 추적 입금까지는 닿게)
REQUEST_GAP = 0.3
DUST = Decimal("1")
FOLLOW_SHARE = Decimal("0.01")
# 그 외 유입이 추적 자금 이상이면(추적 자금이 절반 이하) 남의 자금이 더 많은 지갑 — 유출을 따라가면 리드가 남의 돈으로 불어난다
MIXED_STOP_RATIO = Decimal(1)
MATCH_AMOUNT_TOL = Decimal("0.005")
MATCH_TIME_TOL_MS = 12 * 3600 * 1000
TERMINAL_WORDS = ("binance", "okx", "huobi", "htx", "bybit", "kucoin", "gate", "bitget", "mexc", "poloniex",
                  "exchange", "hot wallet", "deposit")


class ApiError(RuntimeError):
    pass


def _get(path: str, params: dict, retries: int = 6) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    err = ""
    for attempt in range(retries):
        time.sleep(REQUEST_GAP)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise ApiError(f"{path}: HTTP {e.code}") from e
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException, OSError) as e:
            err = str(getattr(e, "reason", e))
        wait = min(30, 2 ** attempt)
        print(f"  tronscan {path}: {err} — {wait}초 후 재시도", file=sys.stderr)
        time.sleep(wait)
    raise ApiError(f"{path}: {retries}회 시도 모두 실패 ({err})")


def _row_tag(v) -> str:
    """TronScan 행의 to_address_tag/from_address_tag는 문자열이거나 {"to_address_tag": …} 꼴의 dict."""
    if isinstance(v, dict):
        return next((str(x) for k, x in v.items() if x and not k.endswith("_logo")), "")
    return str(v or "")


def fetch_address(address: str, get=_get) -> dict:
    """한 주소의 TRC-20 전송 (최대 MAX_PAGES 페이지). 토큰은 transfers()에서 USDT만 거른다.

    contract_address 필터를 붙이면 TronScan이 total을 항상 10,000으로 돌려줘서(실제 26건이어도) 필터 없이 받는다.
    tron.judge는 이 dict만 보고 동작한다.
    """
    raw = {"address": address, "fetchedAt": int(time.time() * 1000), "transfers": [], "tags": {}, "contracts": [],
           "total": 0, "truncated": False}
    contracts: set[str] = set()
    for n in range(MAX_PAGES):
        page = get("/token_trc20/transfers", {"relatedAddress": address, "limit": PAGE, "start": n * PAGE})
        rows = page.get("token_transfers") or []
        raw["transfers"] += rows
        raw["total"] = max(raw["total"], int(page.get("total") or 0))
        for r in rows:
            for side in ("to", "from"):
                tag = _row_tag(r.get(f"{side}_address_tag"))
                if tag:
                    raw["tags"][r[f"{side}_address"]] = tag
                if r.get(f"{side}AddressIsContract"):
                    contracts.add(r[f"{side}_address"])
        if len(rows) < PAGE or (n + 1) * PAGE >= raw["total"]:
            break
    else:
        raw["truncated"] = True
    raw["contracts"] = sorted(contracts)
    return raw


@dataclass
class Transfer:
    time: int
    tx: str
    frm: str
    to: str
    amount: Decimal
    token: str


def transfers(raw: dict) -> list[Transfer]:
    out: dict[tuple, Transfer] = {}
    for t in raw.get("transfers", []):
        tok = TOKENS.get(t.get("contract_address", ""))
        if not tok or t.get("contractRet", "SUCCESS") != "SUCCESS" or t.get("revert"):
            continue
        out[(t["transaction_id"], t["from_address"], t["to_address"], t["quant"])] = Transfer(
            int(t["block_ts"]), t["transaction_id"].lower(), t["from_address"], t["to_address"],
            Decimal(t["quant"]) / Decimal(10) ** tok[1], tok[0])
    return sorted(out.values(), key=lambda t: t.time)


@dataclass
class TronAccount:
    address: str
    hop: int
    raw: dict
    parents: set[str] = field(default_factory=set)
    traced_txs: set[str] = field(default_factory=set)
    expect: list[tuple[Decimal, int]] = field(default_factory=list)
    level: int = 0
    path_level: int = 0
    basis: list[str] = field(default_factory=list)
    outs: list[Transfer] = field(default_factory=list)
    traced_total: Decimal = Decimal(0)
    traced_last: int = 0
    leads: list[Lead] = field(default_factory=list)
    stop: str | None = None
    mix_ratio: Decimal = Decimal(0)   # 추적 자금 대비 그 외 유입(이전 잔액 포함) 비율
    seed_amount: Decimal = Decimal(0)  # hop 0: 브리지 기록상 도착 금액 (추적 입금을 못 찾았을 때 리드 금액)
    seed_time: int = 0

    @property
    def tag(self) -> str:
        return self.raw.get("tags", {}).get(self.address, "")


def _norm_tx(tx: str) -> str:
    return tx.lower().removeprefix("0x")


def judge(acct: TronAccount, parent_level: int) -> None:
    me = acct.address
    ts = transfers(acct.raw)
    ins = [t for t in ts if t.to == me and t.amount >= DUST]
    outs = [t for t in ts if t.frm == me]
    traced = [t for t in ins if _norm_tx(t.tx) in acct.traced_txs]
    matched = 0
    for amount, when in acct.expect:   # 도착 tx가 없는 브리지 도착분
        cands = [t for t in ins if t not in traced and abs(t.amount - amount) <= amount * MATCH_AMOUNT_TOL
                 and abs(t.time - when) <= MATCH_TIME_TOL_MS]
        if cands:
            traced.append(min(cands, key=lambda t: abs(t.time - when)))
            matched += 1
    if matched:
        acct.basis.append(f"추적 입금 {matched}건을 금액·시각으로 찾음 (도착 tx 없음)")
    if not traced:
        acct.level = acct.path_level = 0
        acct.outs = [t for t in outs if t.amount >= DUST]
        acct.basis.append("추적 입금을 이 주소 기록에서 찾지 못함 — 유출 전체를 후보로 봄"
                          + (f" (전송 {acct.raw['total']:,}건 중 최근 {len(acct.raw['transfers']):,}건만 조회)"
                             if acct.raw.get("truncated") else ""))
        return
    start = min(t.time for t in traced)
    acct.traced_total = sum((t.amount for t in traced), Decimal(0))
    acct.traced_last = max(t.time for t in traced)
    acct.outs = [t for t in outs if t.time >= start and t.amount >= DUST]
    end = max((t.time for t in acct.outs), default=max(t.time for t in ts))
    other = [t for t in ins if t not in traced and start <= t.time <= end]
    prev_net = sum((t.amount for t in ts if t.time < start and t.to == me), Decimal(0)) \
        - sum((t.amount for t in ts if t.time < start and t.frm == me), Decimal(0))
    mix = sum((t.amount for t in other), Decimal(0)) + max(prev_net, Decimal(0))
    ratio = acct.mix_ratio = mix / acct.traced_total
    acct.basis.append(f"추적 자금 유입 {len(traced)}건 ({acct.traced_total:,.2f} USDT)")
    if acct.raw.get("truncated"):
        acct.basis.append("전송이 많아 오래된 기록 일부 미수집 — 원래 있던 자금 여부 불명")
        ratio = acct.mix_ratio = max(ratio, MIXING_THRESHOLD)
    if prev_net > DUST:
        acct.basis.append(f"추적 입금 이전 순잔액 {prev_net:,.2f} USDT")
    elif not any(t.time < start for t in ts):
        acct.basis.append("추적 입금 이전 USDT 거래 없음 (새 주소)")
    if other:
        acct.basis.append(f"그 외 유입 {len(other)}건 ({sum((t.amount for t in other), Decimal(0)):,.2f} USDT, {ratio * 100:.2f}%)")
    acct.level = 1 if ratio >= MIXING_THRESHOLD else (3 if len(traced) == 1 and not other else 2)
    acct.basis.append(f"주소 구간 {LEVEL_NAME[acct.level]}")
    acct.path_level = min(parent_level, acct.level)


def seeds_from_leads(leads: list[Lead]) -> dict[str, dict]:
    """브리지 도착 리드(chain=tron)를 받는 주소별로 묶는다."""
    from .bridges import chain_address
    seeds: dict[str, dict] = {}
    for l in leads:
        if l.chain != "tron" or not l.address:
            continue
        addr = l.address
        if addr.startswith("0x") and len(addr) in (42, 44):   # hex(0x41… 또는 0x…) → base58
            addr = chain_address("tron", bytes.fromhex(addr[2:]).rjust(32, b"\0"))
        s = seeds.setdefault(addr, {"txs": set(), "expect": [], "level": 0, "sources": set(),
                                    "amount": Decimal(0), "time": l.time})
        if l.tx:
            s["txs"].add(_norm_tx(l.tx))
        else:
            s["expect"].append((l.amount, l.time))
        s["level"] = max(s["level"], l.level)
        s["sources"].add(l.source.lower())
        s["amount"] += l.amount
        s["time"] = min(s["time"], l.time)
    return seeds


def tron_stage(seeds: dict[str, dict], fetch: Callable[[str], dict], hops: int = 2, max_addresses: int = 30,
               log=print) -> tuple[dict[str, TronAccount], list[dict], list[tuple[str, str]]]:
    """브리지 도착 주소에서 시작해 USDT 송금을 hops단계까지 따라간다. 돌려주는 것: 주소들, 간선, 수집 실패."""
    accounts: dict[str, TronAccount] = {}
    edges: list[dict] = []
    failed: list[tuple[str, str]] = []
    queue = [(a, 0) for a in seeds]
    planned = set(seeds)
    pending = {a: {"parents": set(s["sources"]), "txs": set(s["txs"]), "expect": list(s["expect"]), "level": s["level"]}
               for a, s in seeds.items()}
    while queue:
        addr, hop = queue.pop(0)
        p = pending[addr]
        log(f"[TRON hop {hop}] {addr} ({len(accounts) + 1}/{max_addresses}) 수집 중…")
        try:
            raw = fetch(addr)
        except ApiError as e:
            log(f"  실패: {e}")
            failed.append((addr, str(e)))
            continue
        acct = TronAccount(addr, hop, raw, p["parents"], p["txs"], p["expect"])
        if hop == 0:
            acct.seed_amount, acct.seed_time = seeds[addr].get("amount", Decimal(0)), seeds[addr].get("time", 0)
        accounts[addr] = acct
        judge(acct, p["level"])
        if acct.tag:
            acct.basis.insert(0, f"태그: {acct.tag}")
        if addr in raw.get("contracts", []):
            # Tron에서는 수수료 대납(GasFree 등) 스마트 계정도 컨트랙트로 나온다 — 지갑처럼 쓰이므로 멈추지 않는다
            acct.basis.append("스마트 계정 (컨트랙트로 생성된 주소)")
        if any(w in acct.tag.lower() for w in TERMINAL_WORDS):
            acct.stop = "거래소"
        elif raw.get("truncated") and not acct.traced_total:
            acct.stop = "허브"
        elif acct.mix_ratio >= MIXED_STOP_RATIO:
            acct.stop = "혼합 주소"
        if acct.stop:
            why = {"허브": f"전송 {raw['total']:,}건 허브 (추적 입금이 최근 기록에 없음)",
                   "혼합 주소": f"추적 자금이 유입의 {100 / (1 + acct.mix_ratio):.0f}%뿐인 혼합 주소 (남의 자금이 더 많음)",
                   }.get(acct.stop, f"거래소 주소 ({acct.tag})")
            acct.basis.append(f"{why} — 추적 종료")
            # 금액은 루프 뒤에 부모 → 이 주소 송금 합계로 다시 맞춘다. 여기서는 아는 값으로 채워 둔다
            acct.leads.append(Lead("tron", addr, "", "USDT", acct.traced_total or acct.seed_amount, None,
                                   acct.traced_last or acct.seed_time, addr,
                                   f"{acct.stop} 도달" + (f" ({acct.tag})" if acct.tag else ""), acct.path_level))
            continue
        for t in acct.outs:
            if acct.traced_total and t.amount < acct.traced_total * FOLLOW_SHARE:
                continue   # 수수료 대납(1.5 USDT 등)·잔돈
            tag = raw.get("tags", {}).get(t.to, "")
            edge = {"from": addr, "to": t.to, "asset": t.token, "amount": t.amount, "time": t.time, "tx": t.tx,
                    "label": tag}
            edges.append(edge)
            if any(w in tag.lower() for w in TERMINAL_WORDS):
                edge["result"] = "거래소"
                acct.leads.append(Lead("tron", t.to, t.tx, t.token, t.amount, None, t.time, addr,
                                       f"거래소 입금 ({tag})", acct.path_level))
            elif t.to in planned or (hop < hops and len(planned) < max_addresses):
                edge["result"] = "추적"
                if t.to not in planned:
                    planned.add(t.to)
                    queue.append((t.to, hop + 1))
                    pending[t.to] = {"parents": set(), "txs": set(), "expect": [], "level": 0}
                if t.to not in accounts:
                    q = pending[t.to]
                    q["parents"].add(addr)
                    q["txs"].add(_norm_tx(t.tx))
                    q["level"] = max(q["level"], acct.path_level)
            else:
                edge["result"] = "리드"
                acct.leads.append(Lead("tron", t.to, t.tx, t.token, t.amount, None, t.time, addr,
                                       "미추적 USDT 송금 (홉·주소 상한)", acct.path_level))
        # 아직 옮기지 않은 USDT (추적 자금의 1% 이상)
        if not raw.get("truncated"):
            ts_all = transfers(raw)
            bal = sum((t.amount for t in ts_all if t.to == addr), Decimal(0)) - sum((t.amount for t in ts_all if t.frm == addr), Decimal(0))
            base = acct.traced_total or acct.seed_amount
            if base and bal >= base * FOLLOW_SHARE:
                acct.leads.append(Lead("tron", addr, "", "USDT", bal, None, raw.get("fetchedAt", 0), addr,
                                       "잔액 보유 (조회 시점)", acct.path_level))
    rejudge(accounts, edges, pending)
    # 종착 리드 금액: 추적 주소들이 그 주소로 보낸 합계 (hop 0은 브리지 도착 금액 그대로)
    for acct in accounts.values():
        if acct.stop:
            got = sum((e["amount"] for e in edges if e["to"] == acct.address), Decimal(0))
            if got:
                acct.leads[0].amount = got
    if len(planned) >= max_addresses:
        log(f"Tron 주소 상한 {max_addresses}개 도달 — 나머지는 리드로 남김 (--max-tron으로 조정)")
    return accounts, edges, failed


def rejudge(accounts: dict[str, TronAccount], edges: list[dict], pending: dict[str, dict]) -> None:
    """BFS상 먼저 판정된 주소에 다른 갈래의 추적 자금이 나중에 들어왔으면 보태서 다시 판정 (eth.rejudge와 같음).
    이미 멈춘 주소의 유출을 새로 따라가지는 않는다 — 판정·근거·확실도만 갱신."""
    for e in edges:
        tgt = accounts.get(e["to"])
        if e.get("result") == "추적" and tgt and _norm_tx(e["tx"]) not in tgt.traced_txs:
            tgt.traced_txs.add(_norm_tx(e["tx"]))
            tgt.parents.add(e["from"])
    for acct in sorted(accounts.values(), key=lambda a: a.hop):
        seed_level = pending.get(acct.address, {}).get("level", 0) if acct.hop == 0 else 0
        parent_level = max([accounts[p].path_level for p in acct.parents if p in accounts] + [seed_level])
        keep = [b for b in acct.basis if b.endswith("추적 종료") or b.startswith("태그:")]
        acct.basis, acct.traced_total = [], Decimal(0)
        judge(acct, parent_level)
        acct.basis = [b for b in keep if b.startswith("태그:")] + acct.basis + [b for b in keep if b.endswith("추적 종료")]
        for lead in acct.leads:
            lead.level = acct.path_level
