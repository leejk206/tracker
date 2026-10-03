"""원자료 → 정규화된 입출금 이벤트 + 체결 요약 + 1:1 연결 판정.

네트워크를 쓰지 않는다. api.fetch_all()의 dict만 입력으로 받는다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

# 외부 유입이 Unit 입금 가치의 이 비율 미만이면 "소액 혼입"으로 보고 판정을 내리지 않는다.
# 사례 1에서 형제 계정끼리 주고받은 34.78 USDC(입금 수십만 달러 대비 0.02%) 같은 경우.
MIXING_THRESHOLD = Decimal("0.01")
# 입금 토큰 잔액이 입금량의 이 비율 이하면 주문 단위 반올림 잔량으로 본다.
RESIDUAL_THRESHOLD = Decimal("0.001")
HL_FILL_HISTORY_CAP = 10_000

UNIT_DECIMALS = {"btc": 8, "eth": 18, "sol": 9}


def ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def dec(x) -> Decimal:
    return Decimal(str(x)) if x not in (None, "") else Decimal(0)


@dataclass
class Event:
    time: int
    hash: str
    kind: str          # 원장 delta.type
    direction: str     # in / out / internal
    token: str
    amount: Decimal
    usdc_value: Decimal | None
    counterparty: str
    nonce: int | None
    unit: dict | None = None   # 매칭된 HyperUnit operation

    @property
    def label(self) -> str:
        if self.unit:
            return "Unit 입금" if self.direction == "in" else "Unit 출금"
        return {
            ("send", "in"): "HL 송금 수신", ("send", "out"): "HL 송금",
            ("spotTransfer", "in"): "HL 현물 수신", ("spotTransfer", "out"): "HL 현물 송금",
            ("deposit", "in"): "Arbitrum USDC 입금", ("withdraw", "out"): "Arbitrum USDC 출금",
        }.get((self.kind, self.direction), f"{self.kind} ({self.direction})")


def normalize(user: str, delta: dict) -> tuple[str, str, Decimal, Decimal | None, str]:
    """delta → (direction, token, amount, usdc_value, counterparty)."""
    me = user.lower()
    t = delta["type"]
    token = delta.get("token", "USDC")
    amount = dec(delta.get("amount", delta.get("usdc", 0)))
    usdc_value = dec(delta["usdcValue"]) if "usdcValue" in delta else (amount if token == "USDC" else None)
    sender = (delta.get("user") or "").lower()
    dest = (delta.get("destination") or "").lower()

    if t == "deposit":
        return "in", "USDC", amount, amount, "Arbitrum 브리지"
    if t == "withdraw":
        return "out", "USDC", amount, amount, "Arbitrum 브리지"
    if t == "accountClassTransfer":
        return "internal", "USDC", amount, amount, "현물↔선물"
    if t == "spotGenesis":
        return "in", token, amount, usdc_value, "genesis"
    if t in ("vaultDeposit", "vaultCreate"):
        return "out", "USDC", amount, amount, delta.get("vault", "vault")
    if t in ("vaultWithdraw", "vaultDistribution", "vaultLeaderCommission"):
        return "in", "USDC", amount, amount, delta.get("vault", "vault")
    if sender and dest:
        if sender == me and dest == me:
            return "internal", token, amount, usdc_value, "본인 계정 간 이동"
        if dest == me:
            return "in", token, amount, usdc_value, sender
        if sender == me:
            return "out", token, amount, usdc_value, dest
    return "internal", token, amount, usdc_value, "-"


def _unit_key(tx_hash: str) -> tuple[str, int] | None:
    """Unit의 HL쪽 tx 표기 '0xADDR:nonce' → (addr, nonce)."""
    if not tx_hash or ":" not in tx_hash or not tx_hash.startswith("0x"):
        return None
    addr, nonce = tx_hash.rsplit(":", 1)
    return (addr.lower(), int(nonce)) if nonce.isdigit() else None


def unit_amount(op: dict) -> Decimal | None:
    d = UNIT_DECIMALS.get(op.get("asset", ""))
    return dec(op["sourceAmount"]) / (Decimal(10) ** d) if d is not None else None


def build_events(raw: dict) -> tuple[list[Event], list[dict]]:
    """원장 이벤트를 만들고 Unit operation을 nonce로 붙인다. 짝 없는 Unit op도 돌려준다."""
    user = raw["user"].lower()
    ops = raw["unit"].get("operations", [])
    by_key: dict[tuple[str, int], dict] = {}
    for op in ops:
        # 입금: HL쪽 기록은 destinationTxHash(Unit 송신자:nonce). 출금: sourceTxHash(계정:nonce).
        side = op["destinationTxHash"] if op["destinationChain"] == "hyperliquid" else op["sourceTxHash"]
        k = _unit_key(side)
        if k:
            by_key[k] = op

    events, used = [], set()
    for row in raw["ledger"]:
        d = row["delta"]
        direction, token, amount, usdc_value, cp = normalize(user, d)
        ev = Event(row["time"], row["hash"], d["type"], direction, token, amount, usdc_value, cp, d.get("nonce"))
        if ev.nonce is not None:
            # 입금이면 송신자가 Unit, 출금이면 송신자가 본인 — 둘 다 delta.user가 키의 주소다.
            k = ((d.get("user") or "").lower(), int(ev.nonce))
            op = by_key.get(k)
            if op and (
                (direction == "in" and op["destinationAddress"].lower() == user)
                or (direction == "out" and op["sourceAddress"].lower() == user)
            ):
                ev.unit = op
                used.add(op["operationId"])
        events.append(ev)
    unmatched = [op for op in ops if op["operationId"] not in used]
    return events, unmatched


@dataclass
class FillGroup:
    pair: str
    side: str
    count: int = 0
    size: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    first: int = 0
    last: int = 0

    @property
    def vwap(self) -> Decimal:
        return self.notional / self.size if self.size else Decimal(0)


def summarize_fills(raw: dict) -> list[FillGroup]:
    names = raw.get("spotPairs", {})
    groups: dict[tuple[str, str], FillGroup] = {}
    for f in raw["fills"]:
        pair = names.get(f["coin"], f["coin"])
        if not f["coin"].startswith("@") and "/" not in pair:
            pair = f"{pair}-PERP"
        side = "매수" if f["side"] == "B" else "매도"
        if f.get("dir") == "Spot Dust Conversion":
            side += " (먼지 전환)"  # HL이 매일 자동 처리하는 소액 잔량 정리. 주문 아님
        g = groups.setdefault((pair, side), FillGroup(pair, side, first=f["time"]))
        sz, px = dec(f["sz"]), dec(f["px"])
        g.count += 1
        g.size += sz
        g.notional += sz * px
        g.fee += dec(f.get("fee"))
        g.first = min(g.first, f["time"])
        g.last = max(g.last, f["time"])
    return sorted(groups.values(), key=lambda g: g.first)


@dataclass
class Verdict:
    level: str                  # 확정 / 계정 단위 확정 / 추정 / 해당 없음
    summary: str
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # 일괄 요약에서 계정을 가르는 짧은 표식


def token_flow(raw: dict, events: list[Event], token: str) -> dict[str, Decimal]:
    """한 토큰의 계정 수지. 유입(입금+매수+기타 수신) = 유출(매도+송금·출금) + 잔액이어야 한다."""
    names = raw.get("spotPairs", {})
    sold = bought = Decimal(0)
    for f in raw["fills"]:
        pair = names.get(f["coin"], f["coin"])
        if pair.split("/")[0] != token:
            continue
        sz = dec(f["sz"])
        if f["side"] == "B":
            # 매수 수수료는 산 토큰으로 떼인다
            bought += sz - (dec(f.get("fee")) if f.get("feeToken") == token else 0)
        else:
            sold += sz
    flow = {
        "unit_in": sum((e.amount for e in events if e.unit and e.direction == "in" and e.token == token), Decimal(0)),
        "other_in": sum((e.amount for e in events if not e.unit and e.direction == "in" and e.token == token), Decimal(0)),
        "bought": bought,
        "sold": sold,
        "out": sum((e.amount for e in events if e.direction == "out" and e.token == token), Decimal(0)),
        "balance": next((dec(b["total"]) for b in raw.get("balances", []) if b["coin"] == token), Decimal(0)),
    }
    flow["gap"] = flow["unit_in"] + flow["other_in"] + flow["bought"] - flow["sold"] - flow["out"] - flow["balance"]
    return flow


def judge(raw: dict, events: list[Event], groups: list[FillGroup]) -> Verdict:
    """수기 사례 정리의 계정 단위 1:1 판정 기준을 코드로 옮긴 것."""
    unit_in = [e for e in events if e.unit and e.direction == "in"]
    other_in = [e for e in events if e.direction == "in" and not e.unit]
    outs = [e for e in events if e.direction == "out"]
    if not unit_in:
        return Verdict("해당 없음", "HyperUnit 입금 기록이 없음",
                       [f"원장 이벤트 {len(events)}건 중 Unit operation과 nonce가 일치하는 입금 0건"])

    reasons, notes, tags = [], [], []
    unit_value = sum((e.usdc_value or 0 for e in unit_in), Decimal(0))
    known_other = [e for e in other_in if e.usdc_value is not None]
    unknown_other = [e for e in other_in if e.usdc_value is None]
    other_value = sum((e.usdc_value for e in known_other), Decimal(0))
    ratio = other_value / unit_value if unit_value else Decimal(0)

    by_asset = defaultdict(lambda: [0, Decimal(0)])
    for e in unit_in:
        by_asset[e.token][0] += 1
        by_asset[e.token][1] += e.amount
    dep_txt = ", ".join(f"{t} {n}건 {fmt(a)}" for t, (n, a) in by_asset.items())
    reasons.append(f"Unit 입금 {len(unit_in)}건 ({dep_txt}), 원장 nonce ↔ Unit operation 전건 일치")

    if other_in:
        reasons.append(
            f"Unit 외 유입 {len(other_in)}건, 약 {fmt(other_value, 2)} USDC "
            f"(Unit 입금 가치 {fmt(unit_value, 2)} USDC의 {fmt(ratio * 100, 2)}%)"
        )
    else:
        reasons.append("Unit 외 유입 0건")
    if unknown_other:
        notes.append(f"가치 미상 유입 {len(unknown_other)}건: " +
                     ", ".join(sorted({e.token for e in unknown_other})))

    mixed = ratio >= MIXING_THRESHOLD or bool(unknown_other)
    if other_in and not mixed:
        tags.append("소액 외부 유입")

    # 입금 토큰이 계정에 얼마나 남았는지 (전량 전환 여부) + 수지 검산
    for tok, (_, amt) in by_asset.items():
        flow = token_flow(raw, events, tok)
        left = flow["balance"]
        if left == 0:
            reasons.append(f"{tok} 잔액 0 — 입금분 전량 소진")
        elif left <= amt * RESIDUAL_THRESHOLD:
            reasons.append(f"{tok} 잔액 {fmt(left)} — 입금량의 {fmt(left / amt * 100, 3)}%, 주문 단위 반올림 잔량")
            tags.append("반올림 잔량")
        else:
            reasons.append(f"{tok} 잔액 {fmt(left)} — 입금량의 {fmt(left / amt * 100, 1)}%가 아직 계정에 남음")
            tags.append("미전환 잔액")
        rebought = sum((g.size for g in groups if g.pair.startswith(tok + "/") and g.side == "매수"), Decimal(0))
        if rebought > 0:
            notes.append(f"{tok}를 다시 매수한 체결 있음 ({fmt(rebought)}) — 매도·재매수가 섞임")
            tags.append("입금 토큰 재매수")
        if abs(flow["gap"]) > amt * RESIDUAL_THRESHOLD:
            notes.append(
                f"{tok} 수지 불일치 {fmt(flow['gap'])} (입금 {fmt(flow['unit_in'])} + 기타 수신 {fmt(flow['other_in'])} "
                f"+ 매수 {fmt(flow['bought'])} − 매도 {fmt(flow['sold'])} − 유출 {fmt(flow['out'])} − 잔액 {fmt(left)}) "
                "— 체결 기록이 잘렸을 가능성. 매매 구간 수치는 하한값으로 볼 것"
            )
            tags.append("기록 누락 의심")

    if any(g.pair.endswith("-PERP") for g in groups):
        notes.append("선물 체결 있음 — 손익이 다른 거래 상대 자금과 섞일 수 있음")
        tags.append("선물 거래")
    if len(raw["fills"]) >= HL_FILL_HISTORY_CAP:
        notes.append(f"체결 {len(raw['fills']):,}건 — HL은 오래된 체결 조회를 제한하므로 초기 체결이 빠졌을 수 있음")
    if not outs:
        notes.append("유출 기록 없음 — 자금이 아직 계정 안에 있음")
        tags.append("유출 없음")

    if mixed:
        return Verdict("추정", "Unit 외 자금이 섞여 입금 → 출금 연결은 금액·시간 기반 추정", reasons, notes, tags)
    if len(unit_in) == 1:
        return Verdict("확정", "입금 1건이 계정의 유일한 자금원 → 계정의 매매·출금과 1:1 연결", reasons, notes, tags)
    sources = {e.unit.get("sourceAddress") for e in unit_in}
    reasons.append(f"Unit 입금의 원천 체인 송신 주소 {len(sources)}개")
    return Verdict(
        "계정 단위 확정",
        f"입금 {len(unit_in)}건이 계정의 유일한 자금원 → 계정 출금 전체는 이 입금들에서 나옴. "
        "단, 계정 안에서 합쳐지므로 입금 건별 ↔ 출금 건별 매핑은 추정",
        reasons, notes, tags,
    )


def fmt(x, places: int | None = None) -> str:
    x = dec(x)
    if places is not None:
        return f"{x:,.{places}f}"
    s = f"{x.normalize():,f}"
    return s


def analyze(raw: dict) -> dict:
    events, unmatched = build_events(raw)
    groups = summarize_fills(raw)
    verdict = judge(raw, events, groups)
    return {"raw": raw, "events": events, "unmatched_unit": unmatched, "fills": groups, "verdict": verdict}
