"""Ethereum 구간 추적: HL → ETH 출금 주소에서 시작해 ETH·주요 스테이블 흐름을 홉 단위로 따라간다.

네트워크는 fetch 함수(blockscout.fetch_address)에만 있다. 나머지는 저장된 dict만 보고 동작한다.

실데이터에서 확인한 노이즈와 처리:
- 가짜 토큰: 심볼 "ETH"인 ERC-20이 대량으로 찍히고 평판도 "ok" → 토큰은 컨트랙트 주소 화이트리스트만 인정.
- 주소 오염(address poisoning): 실제 상대방과 앞·뒤 4자리가 같은 주소가 먼지 금액을 수백 건 보낸다
  → 유입에서 제외하고 건수만 센다. 그 주소로 실제 송금하면 피해 의심으로 표시.
- DEX 스왑: ETH → USDT처럼 자산만 바뀐다 → 종착점이 아니라 전환. 스왑 대금 수령은 추적 자금으로 잇는다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from . import blockscout
from .blockscout import WHITELIST
from .core import LEVEL_NAME, MIXING_THRESHOLD, Lead

# 같은 묶음끼리 혼입 비율을 비교한다 (WETH = ETH, 스테이블끼리 1:1)
GROUP = {"ETH": "ETH", "WETH": "ETH", "USDC": "USD", "USDT": "USD", "DAI": "USD"}
DUST = {"ETH": Decimal("0.001"), "USD": Decimal("1")}
# 오염 주소가 보내는 금액은 먼지 수준. 닮은 주소라도 이보다 크면 실제 거래로 본다.
POISON_MAX = {"ETH": Decimal("0.1"), "USD": Decimal("100")}
# 추적 자금 대비 이 비율 미만의 유출은 가스비·잔돈으로 보고 따라가지 않는다
FOLLOW_SHARE = Decimal("0.01")
# HyperUnit Ethereum 출금 주소: Treasury(태그 있음)와 Treasury가 호출하는 배치 컨트랙트(태그 없음)
UNIT_SENDERS = {"0xbea9f7fd27f4ee20066f18def0bc586ec221055a", "0x4bbe9b84aac9804557e8a90b7186324f20357e5c"}
# tx 해시 없는 리드(Unit 기록에 ETH tx 미기재)는 금액·시각으로 입금을 찾는다
MATCH_AMOUNT_TOL = Decimal("0.005")
MATCH_TIME_TOL_MS = 12 * 3600 * 1000

TERMINAL = {   # 태그·이름에 이 단어가 있으면 그 주소에서 멈춘다 (DEX·스테이킹은 멈추지 않고 전환·예치로 처리)
    "스테이킹": ("batchdepositor", "beacon deposit", "eth2 deposit", "depositcontract", "lido", "rocket pool",
              "stakewise", "kiln", "figment", "staking"),
    "거래소": ("exchange", "binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "htx", "huobi", "gate.io",
             "bitfinex", "bitget", "mexc", "crypto.com", "upbit", "bithumb", "cex"),
    "믹서": ("tornado", "mixer", "railgun"),
    "브리지": ("bridge", "wormhole", "stargate", "across", "hop protocol", "layerzero", "thorchain", "usdtoft",
             "usdt0", "oftadapter", "debridge"),
    "DEX": ("uniswap", "1inch", "cow protocol", "cowswap", "gpv2", "0x:", "paraswap", "sushiswap", "curve",
            "router", "kyberswap", "odos"),
}
PASS_THROUGH = {"DEX", "스테이킹"}   # 이 종류의 주소는 종착이 아니다 (자금이 형태를 바꿔 돌아온다)


@dataclass
class Transfer:
    time: int
    block: int
    tx: str
    frm: str
    to: str
    asset: str
    amount: Decimal
    kind: str      # tx / internal / token

    @property
    def group(self) -> str:
        return GROUP[self.asset]


@dataclass
class Label:
    name: str = ""
    tags: list[str] = field(default_factory=list)
    is_contract: bool = False

    @property
    def text(self) -> str:
        return " · ".join(dict.fromkeys(self.tags + ([self.name] if self.name else [])))

    def kind(self) -> str | None:
        hay = self.text.lower().replace(" ", "")
        return next((k for k, words in TERMINAL.items() if any(w.replace(" ", "") in hay for w in words)), None)


def to_label(o: dict | None) -> Label:
    if not o:
        return Label()
    tags = [t["name"].strip() for t in ((o.get("metadata") or {}).get("tags") or [])
            if t.get("tagType") == "name" and t.get("name")]
    tags += [t["display_name"] for t in o.get("public_tags") or [] if t.get("display_name")]
    return Label(o.get("name") or "", tags, bool(o.get("is_contract")))


def labels(raw: dict) -> dict[str, Label]:
    """v2 응답의 주소 객체마다 이름·태그·컨트랙트 여부가 들어 있다. 태그가 있는 쪽을 우선."""
    out: dict[str, Label] = {}
    objs = [raw.get("info") or {}]
    for row in raw.get("txs", []) + raw.get("internal", []) + raw.get("tokens", []):
        objs += [row.get("from"), row.get("to")]
    for o in objs:
        if o and o.get("hash"):
            lbl, key = to_label(o), o["hash"].lower()
            if key not in out or (lbl.text and not out[key].text):
                out[key] = lbl
    return out


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _a(o: dict | None) -> str:
    return ((o or {}).get("hash") or "").lower()


def transfers(raw: dict) -> list[Transfer]:
    """v2 원자료 → 실제 자산 이동만 (실패·0원·화이트리스트 밖 토큰 제외)."""
    out: dict[tuple, Transfer] = {}
    for t in raw.get("txs", []):
        v = int(t.get("value") or 0)
        if v > 0 and t.get("result") == "success" and t.get("to"):
            out[(t["hash"].lower(), "tx")] = Transfer(_ms(t["timestamp"]), int(t["block_number"]), t["hash"].lower(),
                                                      _a(t["from"]), _a(t["to"]), "ETH", Decimal(v) / 10 ** 18, "tx")
    for t in raw.get("internal", []):
        v = int(t.get("value") or 0)
        if v > 0 and t.get("success") and t.get("to"):
            tx = t["transaction_hash"].lower()
            out[(tx, "internal", t.get("index"))] = Transfer(_ms(t["timestamp"]), int(t["block_number"]), tx,
                                                             _a(t["from"]), _a(t["to"]), "ETH", Decimal(v) / 10 ** 18,
                                                             "internal")
    for t in raw.get("tokens", []):
        tok = WHITELIST.get(((t.get("token") or {}).get("address_hash") or "").lower())
        if not tok or (t.get("total") or {}).get("value") is None:
            continue
        sym, dec = tok
        tx = t["transaction_hash"].lower()
        out[(tx, "token", t.get("log_index"))] = Transfer(_ms(t["timestamp"]), int(t["block_number"]), tx,
                                                          _a(t["from"]), _a(t["to"]), sym,
                                                          Decimal(t["total"]["value"]) / Decimal(10) ** dec, "token")
    return sorted(out.values(), key=lambda t: (t.block, t.time))


def lookalike(a: str, b: str) -> bool:
    """주소 오염용 닮은 주소: 앞 4자리·뒤 4자리가 같고 주소는 다름."""
    return a != b and a[2:6] == b[2:6] and a[-4:] == b[-4:]


def fmt(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.000001')).normalize():,f}"


@dataclass
class EthAccount:
    address: str
    hop: int
    raw: dict
    parents: set[str] = field(default_factory=set)       # 추적 자금을 보낸 주소 (hop 0은 HL 계정)
    traced_txs: set[str] = field(default_factory=set)    # 추적 자금이 들어온 tx
    expect: list[tuple[Decimal, int]] = field(default_factory=list)   # tx 없는 리드의 (금액, 시각)
    label: Label = field(default_factory=Label)
    level: int = 0
    path_level: int = 0
    basis: list[str] = field(default_factory=list)
    outs: list[Transfer] = field(default_factory=list)   # 추적 시작 이후 실제 유출
    swaps_in: list[Transfer] = field(default_factory=list)
    returned: list[Transfer] = field(default_factory=list)
    traced_total: dict[str, Decimal] = field(default_factory=dict)
    traced_last: int = 0
    noise_senders: set[str] = field(default_factory=set)
    leads: list[Lead] = field(default_factory=list)
    stop: str | None = None   # 종착 사유 (거래소·브리지·믹서·컨트랙트·허브)

    @property
    def key(self) -> str:
        return self.address.lower()


def _match_expected(ins: list[Transfer], expect: list[tuple[Decimal, int]]) -> list[Transfer]:
    found = []
    for amount, when in expect:
        cands = [t for t in ins if t.group == "ETH" and t not in found and abs(t.amount - amount) <= amount * MATCH_AMOUNT_TOL
                 and abs(t.time - when) <= MATCH_TIME_TOL_MS]
        if cands:
            found.append(min(cands, key=lambda t: abs(t.time - when)))
    return found


def judge(acct: EthAccount, parent_level: int) -> None:
    """추적 자금 외 유입(이전 잔액 포함)이 얼마나 섞였는지로 판정 (HL 판정과 같은 1% 기준)."""
    me = acct.key
    lbls = labels(acct.raw)
    ts = transfers(acct.raw)
    ins = [t for t in ts if t.to == me]
    outs = [t for t in ts if t.frm == me]
    others = {t.frm for t in ins} | {t.to for t in outs} | {me}

    def is_noise(t: Transfer) -> bool:
        if t.amount < DUST[t.group]:
            return True
        return t.amount < POISON_MAX[t.group] and any(lookalike(t.frm, c) for c in others)

    noise = [t for t in ins if is_noise(t)]
    acct.noise_senders = {t.frm for t in noise}
    real_in = [t for t in ins if t not in noise]
    traced = [t for t in real_in if t.tx in acct.traced_txs]
    if not traced and acct.expect:
        traced = _match_expected(real_in, acct.expect)
        if traced:
            acct.basis.append(f"추적 입금 {len(traced)}건을 금액·시각으로 찾음 (Unit 기록에 ETH tx 없음)")

    if not traced:
        acct.level = acct.path_level = 0
        acct.basis.append("추적 입금을 이 주소 기록에서 찾지 못함 — 유출 전체를 후보로 봄")
        acct.outs = [t for t in outs if t.amount >= DUST[t.group]]
        return

    start = min(t.block for t in traced)
    prev = [t for t in ts if t.block < start]
    acct.outs = [t for t in outs if t.block >= start and t.amount >= DUST[t.group]]
    end = max((t.block for t in acct.outs), default=max(t.block for t in ts))
    # DEX에서 돌려받은 스왑 대금은 추적 자금이 형태만 바뀐 것
    swapped_to = {t.to for t in acct.outs if lbls.get(t.to, Label()).kind() == "DEX"}
    acct.swaps_in = [t for t in real_in if t not in traced and t.block >= start and swapped_to
                     and (lbls.get(t.frm, Label()).kind() == "DEX" or t.frm in swapped_to)]
    # 이 주소가 보낸 추적 자금이 되돌아온 것 (A → B → A)
    sent_to = {}
    for t in acct.outs:
        sent_to.setdefault(t.to, t.block)
    # 브리지에 넣은 뒤 브리지 쪽에서 들어온 돈은 주문 취소 환불 (deBridge는 넣는 곳과 환불하는 컨트랙트가 다르다)
    bridged = min((t.block for t in acct.outs if lbls.get(t.to, Label()).kind() == "브리지"), default=None)
    acct.returned = [t for t in real_in if t not in traced and t not in acct.swaps_in
                     and ((t.frm in sent_to and t.block >= sent_to[t.frm])
                          or (bridged is not None and t.block >= bridged and lbls.get(t.frm, Label()).kind() == "브리지"))]
    for t in traced + acct.swaps_in + acct.returned:
        acct.traced_total[t.group] = acct.traced_total.get(t.group, Decimal(0)) + t.amount
    acct.traced_last = max(t.time for t in traced)

    other = [t for t in real_in if t not in traced and t not in acct.swaps_in and t not in acct.returned
             and start <= t.block <= end]
    prev_net: dict[str, Decimal] = defaultdict(Decimal)
    for t in prev:
        if t.to == me and not is_noise(t):
            prev_net[t.group] += t.amount
        elif t.frm == me:
            prev_net[t.group] -= t.amount
    mix: dict[str, Decimal] = defaultdict(Decimal)
    for t in other:
        mix[t.group] += t.amount
    for g, v in prev_net.items():
        if v > DUST[g]:
            mix[g] += v

    traced_txt = ", ".join(f"{fmt(v)} {g}" for g, v in acct.traced_total.items())
    acct.basis.append(f"추적 자금 유입 {len(traced)}건" + (f" + 스왑 수령 {len(acct.swaps_in)}건" if acct.swaps_in else "")
                      + (f" + 되돌아온 자금 {len(acct.returned)}건" if acct.returned else "")
                      + f" ({traced_txt})")
    prev_real = [t for t in prev if not (t.to == me and is_noise(t))]
    if acct.raw.get("truncated"):
        acct.basis.append(f"이력이 {blockscout.MAX_PAGES * 50:,}건을 넘어 오래된 기록 일부 미수집 — 원래 있던 자금 여부 불명")
    elif prev_real:
        net = ", ".join(f"{fmt(v)} {g}" for g, v in prev_net.items() if v > DUST[g]) or "순잔액 없음"
        acct.basis.append(f"추적 입금 이전 거래 {len(prev_real)}건 ({net})")
    else:
        acct.basis.append("추적 입금 이전 실제 거래 없음 (새 주소)")
    ratios = {g: mix[g] / v for g, v in acct.traced_total.items() if v}
    foreign = {g for g, v in mix.items() if g not in acct.traced_total and v > DUST[g]}
    if other:
        acct.basis.append(f"그 외 유입 {len(other)}건 (" + ", ".join(f"{fmt(v)} {g}" for g, v in mix.items()) + ")"
                          + "".join(f", {g} 기준 {r * 100:.2f}%" for g, r in ratios.items() if r))
    unit_other = [t for t in other if t.frm in UNIT_SENDERS or "unit" in lbls.get(t.frm, Label()).text.lower()]
    if unit_other:
        acct.basis.append(f"그 외 유입 중 {len(unit_other)}건은 HyperUnit 출금 — 추적 범위 밖 다른 HL 계정에서 온 것")
    if noise:
        acct.basis.append(f"주소 오염·먼지 유입 {len(noise)}건 제외")
    mixed = any(r >= MIXING_THRESHOLD for r in ratios.values()) or bool(foreign) or bool(acct.raw.get("truncated"))
    acct.level = 1 if mixed else (3 if len(traced) == 1 and not other and not acct.swaps_in and not acct.returned else 2)
    acct.basis.append(f"주소 구간 {LEVEL_NAME[acct.level]}")
    acct.path_level = min(parent_level, acct.level)


def seeds_from_leads(leads: list[Lead]) -> dict[str, dict]:
    """HL → Ethereum 출금 리드를 목적지 주소별로 묶는다."""
    seeds: dict[str, dict] = {}
    for l in leads:
        if l.chain != "ethereum" or not l.address:
            continue
        s = seeds.setdefault(l.address.lower(), {"address": l.address, "txs": set(), "expect": [], "level": 0,
                                                 "sources": set()})
        if l.tx:
            s["txs"].add(l.tx.lower())
        else:
            s["expect"].append((l.amount, l.time))
        s["level"] = max(s["level"], l.level)
        s["sources"].add(l.source.lower())
    return seeds


def eth_stage(seeds: dict[str, dict], fetch: Callable[[str], dict], hops: int = 2, max_addresses: int = 30,
              log=print) -> tuple[dict[str, EthAccount], list[dict], list[tuple[str, str]]]:
    """HL 출금 주소에서 시작해 ETH 송금을 hops단계까지 따라간다. 돌려주는 것: 주소들, 간선, 수집 실패."""
    accounts: dict[str, EthAccount] = {}
    edges: list[dict] = []
    failed: list[tuple[str, str]] = []
    queue: list[tuple[str, int]] = [(a, 0) for a in seeds]
    planned = set(seeds)
    pending = {a: {"parents": set(s["sources"]), "txs": set(s["txs"]), "expect": list(s["expect"]),
                   "level": s["level"]} for a, s in seeds.items()}

    while queue:
        key, hop = queue.pop(0)
        p = pending[key]
        log(f"[ETH hop {hop}] {key} ({len(accounts) + 1}/{max_addresses}) 수집 중…")
        try:
            raw = fetch(key)
        except blockscout.ApiError as e:
            log(f"  실패: {e}")
            failed.append((key, str(e)))
            continue
        lbls = labels(raw)
        lbl = lbls.get(key, Label())
        acct = EthAccount((raw.get("info") or {}).get("hash") or key, hop, raw, p["parents"],
                          p["txs"], p["expect"], lbl)
        accounts[key] = acct
        judge(acct, p["level"])
        if lbl.text:
            acct.basis.insert(0, f"태그: {lbl.text}")

        kind = lbl.kind()
        if kind and kind not in PASS_THROUGH:
            acct.stop = kind
        elif lbl.is_contract:
            acct.stop = "컨트랙트"
        elif raw.get("truncated") and not acct.traced_total:
            acct.stop = "허브"
        if acct.stop:
            why = f"거래 {blockscout.MAX_PAGES * 50:,}건 이상 허브" if acct.stop == "허브" else f"{acct.stop} 주소"
            acct.basis.append(f"{why} — 추적 종료")
            log(f"  {why} — 추적 종료")
            for g, v in acct.traced_total.items():
                acct.leads.append(Lead("ethereum", acct.address, "", g, v, None, acct.traced_last, acct.address,
                                       f"{acct.stop} 도달" + (f" ({lbl.text})" if lbl.text else ""), acct.path_level))
            continue

        for t in acct.outs:
            total = acct.traced_total.get(t.group)
            if total and t.amount < total * FOLLOW_SHARE:
                continue   # 가스비·잔돈
            to_lbl = lbls.get(t.to, Label())
            edge = {"from": acct.address, "to": t.to, "asset": t.asset, "amount": t.amount, "time": t.time,
                    "tx": t.tx, "kind": t.kind, "label": to_lbl.text}
            edges.append(edge)
            to_kind = to_lbl.kind()
            if t.to in acct.noise_senders:
                acct.basis.append(f"주의: 주소 오염 먼지를 보낸 주소 `{t.to}`로 {fmt(t.amount)} {t.asset} 송금 — 오염 피해 의심")
            if to_kind == "DEX":
                edge["result"] = "스왑"
            elif to_kind == "스테이킹":
                edge["result"] = "스테이킹 예치"
                if not any(b.startswith("스테이킹") for b in acct.basis):
                    acct.basis.append(
                        "스테이킹 예치 — 회수(비콘 체인 출금)는 일반 tx가 아니라 기록에 안 잡힘. 이후 유출로 이어서 추적"
                        + (" (이 주소에 비콘 출금 기록 있음)" if (raw.get("info") or {}).get("has_beacon_chain_withdrawals") else ""))
            elif to_kind or to_lbl.is_contract:
                edge["result"] = to_kind or "컨트랙트"
                acct.leads.append(Lead("ethereum", t.to, t.tx, t.asset, t.amount, None, t.time, acct.address,
                                       f"{edge['result']} 입금" + (f" ({to_lbl.text})" if to_lbl.text else ""),
                                       acct.path_level))
            elif t.to in planned or (hop < hops and len(planned) < max_addresses):
                edge["result"] = "추적"
                if t.to not in planned:
                    planned.add(t.to)
                    queue.append((t.to, hop + 1))
                    pending[t.to] = {"parents": set(), "txs": set(), "expect": [], "level": 0}
                q = pending[t.to]
                if t.to not in accounts:   # 아직 조회 전이면 부모 정보를 보탠다
                    q["parents"].add(acct.key)
                    q["txs"].add(t.tx)
                    q["level"] = max(q["level"], acct.path_level)
            else:
                edge["result"] = "리드"
                acct.leads.append(Lead("ethereum", t.to, t.tx, t.asset, t.amount, None, t.time, acct.address,
                                       "미추적 ETH 송금 (홉·주소 상한)", acct.path_level))

        # 남은 잔액이 추적 자금의 1% 이상이면 리드로
        info = raw.get("info") or {}
        bal = Decimal(int(info.get("coin_balance") or 0)) / 10 ** 18
        eth_in = acct.traced_total.get("ETH")
        if eth_in and bal >= eth_in * FOLLOW_SHARE:
            acct.leads.append(Lead("ethereum", acct.address, "", "ETH", bal, None, raw.get("fetchedAt", 0),
                                   acct.address, "잔액 보유 (조회 시점)", acct.path_level))
    rejudge(accounts, edges, pending)
    if len(planned) >= max_addresses:
        log(f"ETH 주소 상한 {max_addresses}개 도달 — 나머지는 리드로 남김 (--max-eth로 조정)")
    return accounts, edges, failed


def rejudge(accounts: dict[str, EthAccount], edges: list[dict], pending: dict[str, dict]) -> None:
    """BFS 순서상 먼저 판정된 주소에 다른 갈래의 추적 자금이 나중에 들어오면, 그 tx를 추적 입금으로 보태 다시 판정한다.
    경로 확실도는 hop 순서대로 다시 전파한다."""
    for e in edges:
        tgt = accounts.get(e["to"].lower())
        if e.get("result") == "추적" and tgt and e["tx"] not in tgt.traced_txs:
            tgt.traced_txs.add(e["tx"])
            tgt.parents.add(e["from"].lower())
    for acct in sorted(accounts.values(), key=lambda a: a.hop):
        seed_level = pending.get(acct.key, {}).get("level", 0) if acct.hop == 0 else 0
        parent_level = max([accounts[p].path_level for p in acct.parents if p in accounts] + [seed_level])
        stop_lines = [b for b in acct.basis if b.endswith("추적 종료") or b.startswith(("주의:", "스테이킹"))]
        acct.basis, acct.traced_total, acct.swaps_in, acct.returned = [], {}, [], []
        judge(acct, parent_level)
        if acct.label.text:
            acct.basis.insert(0, f"태그: {acct.label.text}")
        acct.basis += stop_lines
        for lead in acct.leads:
            lead.level = acct.path_level
