"""체인 공통 추적 엔진: 주소 BFS · 연결 판정 · 재판정 · 리드.

체인마다 다른 것(원자료 수집, 전송 정규화, 주소 태그, 멈춤 규칙)은 Adapter가 맡는다.
체인 하나 추가 = Adapter 하나 (pipeline/adapters.py). 여기 로직은 모든 체인이 같이 쓴다.

판정 기준 (HL 판정과 같은 1%):
- 추적 자금 = 추적 입금 + 스왑 수령 + 되돌아온 자금(A→B→A, 브리지 환불) + 스테이킹 회수
- 그 외 유입 + 추적 입금 이전 잔액이 추적 자금의 1% 이상이면 `추정`, 아니면 입금 1건 `확정` / 여러 건 `계정 단위 확정`
- 경로 확실도 = 부모 경로 확실도와 이 주소 구간 확실도 중 낮은 것

실데이터에서 확인한 노이즈 처리:
- 주소 오염(address poisoning): 실제 상대방과 앞·뒤 4자리가 같은 주소의 먼지 입금 → 유입에서 제외, 건수만 센다
- DEX 스왑·스테이킹은 종착이 아니라 전환·예치 — 돌아온 자금을 추적 자금으로 잇는다
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .core import LEVEL_NAME, MIXING_THRESHOLD, Lead

FOLLOW_SHARE = Decimal("0.01")          # 추적 자금 대비 이 비율 미만의 유출은 가스비·잔돈으로 보고 따라가지 않는다
MATCH_AMOUNT_TOL = Decimal("0.005")     # tx 없는 리드는 금액(±0.5%)·시각(±12h)으로 입금을 찾는다
MATCH_TIME_TOL_MS = 12 * 3600 * 1000
DEPOSIT_SHARE = Decimal("0.95")         # 태그 없는 거래소 입금 주소: 추적 자금의 95% 이상을
DEPOSIT_WINDOW_MS = 72 * 3600 * 1000    # 72시간 안에 거래소 태그 주소로 보냄
HL_WITHDRAW_FEE = Decimal(1)            # HL → Arbitrum USDC 출금 수수료 (USDC)

TERMINAL = {   # 태그·이름에 이 단어가 있으면 그 주소에서 멈춘다 (DEX·스테이킹은 멈추지 않고 전환·예치로 처리)
    "스테이킹": ("batchdepositor", "beacon deposit", "eth2 deposit", "depositcontract", "lido", "rocket pool",
              "stakewise", "kiln", "figment", "staking"),
    "거래소": ("exchange", "binance", "coinbase", "kraken", "okx", "bybit", "kucoin", "htx", "huobi", "gate.io",
             "bitfinex", "bitget", "mexc", "crypto.com", "upbit", "bithumb", "cex", "hot wallet"),
    "믹서": ("tornado", "mixer", "railgun"),
    "브리지": ("bridge", "wormhole", "stargate", "across", "hop protocol", "layerzero", "thorchain", "usdtoft",
             "usdt0", "oftadapter", "debridge", "unit:",   # "Unit: Treasury" = HyperUnit (다시 HL로 들어감)
             "socket", "li.fi", "lifi", "bungee", "mayan", "symbiosis", "orbiter", "synapse", "celer",
             "squid", "tokenmessenger", "cctp", "deposit bridge"),   # 브리지 묶음 서비스·Circle CCTP·HL 입금 브리지
    "DEX": ("uniswap", "1inch", "cow protocol", "cowswap", "gpv2", "0x:", "paraswap", "sushiswap", "curve",
            "router", "kyberswap", "odos"),
}
PASS_THROUGH = {"DEX", "스테이킹"}   # 이 종류의 주소는 종착이 아니다 (자금이 형태를 바꿔 돌아온다)


@dataclass
class Transfer:
    time: int
    block: int        # 같은 원자료 안에서의 순서 기준 (Tron은 시각)
    tx: str
    frm: str
    to: str
    asset: str
    amount: Decimal
    kind: str         # tx / internal / token / withdrawal
    group: str = ""   # 혼입 비율을 비교하는 묶음 (WETH = ETH, 스테이블끼리 USD)


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


@dataclass
class Adapter:
    """체인 하나의 데이터·규칙. 엔진은 이것만 보고 동작한다."""
    chain: str                                   # 리드·보고서의 체인 이름 ("ethereum", "tron" …)
    name: str                                    # 표시 이름
    fetch: Callable[[str], dict]                 # 주소 → 원자료
    transfers: Callable[[dict], list[Transfer]]  # 원자료 → 실제 자산 이동 (group 채움)
    labels: Callable[[dict], dict[str, Label]]   # 원자료 → 주소 키별 태그
    dust: dict[str, Decimal]                     # 묶음별 먼지 기준
    norm: Callable[[str], str] = str.lower       # 주소 키 (EVM 소문자, Tron 그대로)
    parse: Callable[[str], str] = lambda a: a    # 리드 주소 → 이 체인 표기 (Tron hex → base58)
    display: Callable[[dict, str], str] = lambda raw, key: key
    poison_max: dict[str, Decimal] | None = None   # 주소 오염 판정 상한 (None이면 안 봄)
    stop_at_contract: bool = True                # Tron은 수수료 대납 스마트 계정도 컨트랙트라 멈추지 않는다
    mixed_stop_ratio: Decimal | None = None      # 그 외 유입 ÷ 추적 자금이 이 이상이면 혼합 주소로 멈춤
    balance: Callable[[dict, str], dict[str, Decimal]] | None = None   # 조회 시점 잔액 (묶음별)
    min_balance_lead: dict[str, Decimal] = field(default_factory=dict)
    service_senders: dict[str, str] = field(default_factory=dict)       # 주소 → 서비스 이름 (그 외 유입 설명용)
    errors: tuple = (RuntimeError,)              # 수집 실패로 보고 넘어갈 예외


@dataclass
class Account:
    address: str
    hop: int
    raw: dict
    key: str = ""
    parents: set[str] = field(default_factory=set)       # 추적 자금을 보낸 주소 (hop 0은 이전 단계 계정)
    traced_txs: set[str] = field(default_factory=set)    # 추적 자금이 들어온 tx
    expect: list[tuple[Decimal, int]] = field(default_factory=list)   # tx 없는 리드의 (금액, 시각)
    label: Label = field(default_factory=Label)
    origin: bool = False                                  # 시작 주소: 들어온 자금 전체가 추적 대상
    level: int = 0
    path_level: int = 0
    basis: list[str] = field(default_factory=list)
    outs: list[Transfer] = field(default_factory=list)   # 추적 시작 이후 실제 유출
    swaps_in: list[Transfer] = field(default_factory=list)
    returned: list[Transfer] = field(default_factory=list)
    staking_in: list[Transfer] = field(default_factory=list)
    deposit_of: str = ""                                  # 태그 없는 거래소 입금 주소로 보이면 거래소 이름
    prior_balance: dict[str, Decimal] = field(default_factory=dict)
    traced_total: dict[str, Decimal] = field(default_factory=dict)   # 총액 (혼입 비율 기준)
    traced_net: dict[str, Decimal] = field(default_factory=dict)     # 스왑·스테이킹으로 내보낸 만큼 뺀 순액
    traced_last: int = 0
    mix_ratio: Decimal = Decimal(0)
    noise_senders: set[str] = field(default_factory=set)
    leads: list[Lead] = field(default_factory=list)
    stop: str | None = None
    seed_amount: Decimal = Decimal(0)    # hop 0: 이전 단계 기록상 들어온 금액 (추적 입금을 못 찾았을 때 리드 금액)
    seed_time: int = 0

    def __post_init__(self):
        self.key = self.key or self.address

    @property
    def tag(self) -> str:
        return self.label.text


def lookalike(a: str, b: str) -> bool:
    """주소 오염용 닮은 주소: 앞 4자리·뒤 4자리가 같고 주소는 다름."""
    return a != b and a[2:6] == b[2:6] and a[-4:] == b[-4:]


def fmt(x: Decimal) -> str:
    return f"{x.quantize(Decimal('0.000001')).normalize():,f}"


def _match_expected(ins: list[Transfer], expect: list[tuple[Decimal, int]]) -> list[Transfer]:
    found = []
    for amount, when in expect:
        cands = [t for t in ins if t not in found and abs(t.amount - amount) <= amount * MATCH_AMOUNT_TOL
                 and abs(t.time - when) <= MATCH_TIME_TOL_MS]
        if cands:
            found.append(min(cands, key=lambda t: abs(t.time - when)))
    return found


def judge(acct: Account, parent_level: int, ad: Adapter) -> None:
    """추적 자금 외 유입(이전 잔액 포함)이 얼마나 섞였는지로 이 주소 구간을 판정한다."""
    me = acct.key
    lbls = ad.labels(acct.raw)
    ts = ad.transfers(acct.raw)
    ins = [t for t in ts if t.to == me]
    outs = [t for t in ts if t.frm == me]
    others = {t.frm for t in ins} | {t.to for t in outs} | {me}
    dust = lambda t: ad.dust.get(t.group, Decimal(0))

    def is_noise(t: Transfer) -> bool:
        if t.amount < dust(t):
            return True
        return bool(ad.poison_max) and t.amount < ad.poison_max.get(t.group, Decimal(0)) \
            and any(lookalike(t.frm, c) for c in others)

    noise = [t for t in ins if is_noise(t)]
    acct.noise_senders = {t.frm for t in noise}
    real_in = [t for t in ins if t not in noise]
    if acct.origin:
        traced = list(real_in)
        acct.basis.append("시작 주소 — 들어온 자금 전체를 추적 대상으로 봄")
    else:
        traced = [t for t in real_in if t.tx in acct.traced_txs]
        if acct.expect:   # tx 없는 리드는 tx로 찾은 입금과 별개로 금액·시각으로 찾는다
            extra = _match_expected([t for t in real_in if t not in traced], acct.expect)
            traced += extra
            if extra:
                acct.basis.append(f"추적 입금 {len(extra)}건을 금액·시각으로 찾음 (이전 단계 기록에 이 체인 tx 없음)")

    if not traced:
        acct.outs = [t for t in outs if t.amount >= dust(t)]
        if acct.origin:
            acct.level, acct.path_level = 3, parent_level
            acct.basis.append("들어온 기록 없음 — 유출 전체를 추적")
            return
        acct.level = acct.path_level = 0
        acct.basis.append("추적 입금을 이 주소 기록에서 찾지 못함 — 유출 전체를 후보로 봄"
                          + (" (이력이 많아 최근 기록만 조회)" if acct.raw.get("truncated") else ""))
        return

    start = min(t.block for t in traced)
    prev = [t for t in ts if t.block < start]
    acct.outs = [t for t in outs if t.block >= start and t.amount >= dust(t)]
    end = max((t.block for t in acct.outs), default=max(t.block for t in ts))
    # DEX에서 돌려받은 스왑 대금은 추적 자금이 형태만 바뀐 것
    swapped_to = {t.to for t in acct.outs if lbls.get(t.to, Label()).kind() == "DEX"}
    acct.swaps_in = [t for t in real_in if t not in traced and t.block >= start and swapped_to
                     and (lbls.get(t.frm, Label()).kind() == "DEX" or t.frm in swapped_to)]
    # 이 주소가 보낸 추적 자금이 되돌아온 것 (A → B → A), 브리지 주문 취소 환불 (넣는 곳과 환불하는 컨트랙트가 다르다)
    sent_to: dict[str, int] = {}
    for t in acct.outs:
        sent_to.setdefault(t.to, t.block)
    bridged = min((t.block for t in acct.outs if lbls.get(t.to, Label()).kind() == "브리지"), default=None)
    acct.returned = [t for t in real_in if t not in traced and t not in acct.swaps_in
                     and ((t.frm in sent_to and t.block >= sent_to[t.frm])
                          or (bridged is not None and t.block >= bridged and lbls.get(t.frm, Label()).kind() == "브리지"))]
    staked = min((t.block for t in acct.outs if lbls.get(t.to, Label()).kind() == "스테이킹"), default=None)
    acct.staking_in = [t for t in real_in if t.kind == "withdrawal" and staked is not None and t.block >= staked]
    for t in traced + acct.swaps_in + acct.returned + acct.staking_in:
        acct.traced_total[t.group] = acct.traced_total.get(t.group, Decimal(0)) + t.amount
    acct.traced_last = max(t.time for t in traced)

    other = [t for t in real_in if t not in traced and t not in acct.swaps_in and t not in acct.returned
             and t not in acct.staking_in and start <= t.block <= end]
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
        if v > ad.dust.get(g, Decimal(0)):
            mix[g] += v

    # 순 추적 자금: 스왑으로 내보낸 만큼은 뺀다 (USDC → USDT 스왑이면 둘 다 더해져 두 배로 보이는 것 방지).
    # 혼입 비율은 총액 기준 그대로 — 순액이 작아지면 먼지 입금 하나로도 '추정'이 되어버린다.
    # 스테이킹 예치금은 회수(비콘 출금)를 실제로 받아온 경우에만 뺀다.
    swap_out: dict[str, Decimal] = defaultdict(Decimal)
    for t in acct.outs:
        if t.to in swapped_to or (acct.staking_in and lbls.get(t.to, Label()).kind() == "스테이킹"):
            swap_out[t.group] += t.amount
    acct.traced_net = {g: max(v - swap_out.get(g, Decimal(0)), Decimal(0)) for g, v in acct.traced_total.items()}
    acct.prior_balance = {g: v for g, v in prev_net.items() if v > ad.dust.get(g, Decimal(0))}
    traced_txt = ", ".join(f"{fmt(v)} {g}" for g, v in acct.traced_net.items() if v)
    acct.basis.append(f"추적 자금 유입 {len(traced)}건" + (f" + 스왑 수령 {len(acct.swaps_in)}건" if acct.swaps_in else "")
                      + (f" + 되돌아온 자금 {len(acct.returned)}건" if acct.returned else "")
                      + (f" + 스테이킹 회수 {len(acct.staking_in)}건" if acct.staking_in else "")
                      + f" ({traced_txt}" + (", 스왑·스테이킹으로 내보낸 만큼 뺀 순액" if any(swap_out.values()) else "") + ")")
    prev_real = [t for t in prev if not (t.to == me and is_noise(t))]
    if acct.raw.get("truncated"):
        acct.basis.append("이력이 많아 오래된 기록 일부 미수집 — 원래 있던 자금 여부 불명")
    elif prev_real:
        net = ", ".join(f"{fmt(v)} {g}" for g, v in acct.prior_balance.items()) or "순잔액 없음"
        acct.basis.append(f"추적 입금 이전 거래 {len(prev_real)}건 ({net})")
    else:
        acct.basis.append("추적 입금 이전 실제 거래 없음 (새 주소)")
    ratios = {g: mix[g] / v for g, v in acct.traced_total.items() if v}
    foreign = {g for g, v in mix.items() if g not in acct.traced_total and v > ad.dust.get(g, Decimal(0))}
    if other:
        acct.basis.append(f"그 외 유입 {len(other)}건 (" + ", ".join(f"{fmt(v)} {g}" for g, v in mix.items()) + ")"
                          + "".join(f", {g} 기준 {r * 100:.2f}%" for g, r in ratios.items() if r))
    for name in sorted(set(ad.service_senders.values())):
        n = sum(1 for t in other if ad.service_senders.get(t.frm) == name or name.lower() in lbls.get(t.frm, Label()).text.lower())
        if n:
            acct.basis.append(f"그 외 유입 중 {n}건은 {name} 출금 — 추적 범위 밖 다른 계정에서 온 것")
    if noise:
        acct.basis.append(f"주소 오염·먼지 유입 {len(noise)}건 제외")
    acct.mix_ratio = max(ratios.values(), default=Decimal(0))
    if acct.raw.get("truncated"):
        acct.mix_ratio = max(acct.mix_ratio, MIXING_THRESHOLD)
    mixed = acct.mix_ratio >= MIXING_THRESHOLD or bool(foreign)
    if acct.origin:
        acct.level = 3
    else:
        acct.level = 1 if mixed else (3 if len(traced) == 1 and not (other or acct.swaps_in or acct.returned
                                                                        or acct.staking_in) else 2)
    acct.basis.append(f"주소 구간 {LEVEL_NAME[acct.level]}")
    acct.path_level = min(parent_level, acct.level)


def deposit_address_of(acct: Account, lbls: dict[str, Label]) -> str:
    """태그 없는 거래소 입금 주소: 이전 잔액 없이 받은 추적 자금의 대부분을 곧바로 거래소 핫월렛으로 쓸어 보냈는가.
    돌려주는 값은 거래소 이름 (예: "Binance"), 아니면 ""."""
    if acct.label.text or not acct.outs or not acct.traced_last or acct.prior_balance or acct.raw.get("truncated"):
        return ""
    to_ex = [t for t in acct.outs if lbls.get(t.to, Label()).kind() == "거래소"]
    if not to_ex:
        return ""
    by_group: dict[str, Decimal] = defaultdict(Decimal)
    for t in to_ex:
        if t.time - acct.traced_last <= DEPOSIT_WINDOW_MS:
            by_group[t.group] += t.amount
    if not any(acct.traced_total.get(g) and v >= acct.traced_total[g] * DEPOSIT_SHARE for g, v in by_group.items()):
        return ""
    return lbls[to_ex[0].to].text.split(":")[0].split("·")[0].split("-")[0].strip() or "거래소"


def deposit_note(acct: Account) -> str:
    return (f"{acct.deposit_of} 입금 주소로 보임 — 태그는 없지만 받은 돈의 {DEPOSIT_SHARE * 100:.0f}% 이상을 "
            f"{DEPOSIT_WINDOW_MS // 3600000}시간 안에 {acct.deposit_of} 핫월렛으로 보냄")


def staking_note(acct: Account, ad: Adapter) -> str:
    """스테이킹 예치·회수 요약. 비콘 출금을 받아왔으면 실제 회수액과 보상을, 못 받았으면 그 사실을 적는다."""
    from hl_ledger.analyze import ts
    lbls = ad.labels(acct.raw)
    deposited = sum((t.amount for t in acct.outs if lbls.get(t.to, Label()).kind() == "스테이킹"), Decimal(0))
    back = sum((t.amount for t in acct.staking_in), Decimal(0))
    if acct.staking_in:
        first, last = min(t.time for t in acct.staking_in), max(t.time for t in acct.staking_in)
        cut = " — 출금 기록이 많아 최근 것만 조회, 회수액은 하한값" if acct.raw.get("withdrawals_truncated") else ""
        return (f"스테이킹 예치 {fmt(deposited)} ETH → 비콘 출금 {len(acct.staking_in)}건 {fmt(back)} ETH 회수 "
                f"(예치 대비 {fmt(back - deposited)} ETH, {ts(first)[:10]} ~ {ts(last)[:10]}){cut}")
    if acct.raw.get("withdrawals_error"):
        return f"스테이킹 예치 {fmt(deposited)} ETH — 비콘 출금 조회 실패, 이후 유출로 이어서 추적"
    return f"스테이킹 예치 {fmt(deposited)} ETH — 비콘 출금 기록 없음 (아직 회수 안 함 또는 다른 주소로 회수)"


def seeds_from_leads(leads: list[Lead], ad: Adapter) -> dict[str, dict]:
    """이전 단계 리드 중 이 체인으로 온 것을 받는 주소별로 묶는다."""
    seeds: dict[str, dict] = {}
    for l in leads:
        if l.chain != ad.chain or not l.address:
            continue
        addr = ad.parse(l.address)
        s = seeds.setdefault(ad.norm(addr), {"address": addr, "txs": set(), "expect": [], "level": 0,
                                             "sources": set(), "amount": Decimal(0), "time": l.time})
        if l.kind.startswith("Arbitrum USDC 출금"):
            # HL 출금 리드의 tx는 HL 해시라 이 체인 tx가 아니다 — 수수료 뺀 금액·시각으로 찾는다
            s["expect"].append((l.amount - HL_WITHDRAW_FEE, l.time))
        elif l.tx:
            s["txs"].add(l.tx.lower().removeprefix("0x") if ad.chain == "tron" else l.tx.lower())
        else:
            s["expect"].append((l.amount, l.time))
        s["level"] = max(s["level"], l.level)
        s["sources"].add(l.source.lower())
        s["amount"] += l.amount
        s["time"] = min(s["time"], l.time)
    return seeds


def origin_seeds(addresses: list[str], ad: Adapter) -> dict[str, dict]:
    """--start: 이 주소들에서 시작 (들어온 자금 전체를 추적 대상으로)."""
    return {ad.norm(ad.parse(a)): {"address": ad.parse(a), "txs": set(), "expect": [], "level": 3, "sources": set(),
                                   "amount": Decimal(0), "time": 0, "origin": True} for a in addresses}


def stage(seeds: dict[str, dict], ad: Adapter, hops: int = 2, max_addresses: int = 30, log=print
          ) -> tuple[dict[str, Account], list[dict], list[tuple[str, str]]]:
    """seeds에서 시작해 송금을 hops단계까지 따라간다. 돌려주는 것: 주소들, 간선, 수집 실패."""
    accounts: dict[str, Account] = {}
    edges: list[dict] = []
    failed: list[tuple[str, str]] = []
    queue: list[tuple[str, int]] = [(k, 0) for k in seeds]
    planned = set(seeds)
    pending = {k: {"parents": set(s["sources"]), "txs": set(s["txs"]), "expect": list(s["expect"]), "level": s["level"],
                   "address": s.get("address", k)} for k, s in seeds.items()}

    while queue:
        key, hop = queue.pop(0)
        p = pending[key]
        log(f"[{ad.name} hop {hop}] {p['address']} ({len(accounts) + 1}/{max_addresses}) 수집 중…")
        try:
            raw = ad.fetch(p["address"])
        except ad.errors as e:
            log(f"  실패: {e}")
            failed.append((p["address"], str(e)))
            continue
        lbls = ad.labels(raw)
        lbl = lbls.get(key, Label())
        acct = Account(ad.display(raw, p["address"]), hop, raw, key, p["parents"], p["txs"], p["expect"], lbl,
                       origin=bool(seeds.get(key, {}).get("origin")) and hop == 0)
        if hop == 0:
            acct.seed_amount, acct.seed_time = seeds[key].get("amount", Decimal(0)), seeds[key].get("time", 0)
        accounts[key] = acct
        judge(acct, p["level"], ad)
        if lbl.text:
            acct.basis.insert(0, f"태그: {lbl.text}")
        if lbl.is_contract and not ad.stop_at_contract:
            acct.basis.append("스마트 계정 (컨트랙트로 생성된 주소)")

        kind = lbl.kind()
        if kind and kind not in PASS_THROUGH:
            acct.stop = kind
        elif lbl.is_contract and ad.stop_at_contract:
            acct.stop = "컨트랙트"
        elif raw.get("truncated") and not acct.traced_total and not acct.origin:
            acct.stop = "허브"
        elif ad.mixed_stop_ratio is not None and acct.mix_ratio >= ad.mixed_stop_ratio and not acct.origin:
            acct.stop = "혼합 주소"
        if acct.stop:
            why = {"허브": "이력이 많은 허브 (추적 입금이 조회 범위에 없음)",
                   "혼합 주소": f"추적 자금이 유입의 {100 / (1 + acct.mix_ratio):.0f}%뿐인 혼합 주소 (남의 자금이 더 많음)",
                   }.get(acct.stop, f"{acct.stop} 주소")
            acct.basis.append(f"{why} — 추적 종료")
            log(f"  {why} — 추적 종료")
            amounts = acct.traced_net or acct.traced_total or {"": acct.seed_amount}
            for g, v in amounts.items():
                acct.leads.append(Lead(ad.chain, acct.address, "", g or "-", v, None, acct.traced_last or acct.seed_time,
                                       acct.address, f"{acct.stop} 도달" + (f" ({lbl.text})" if lbl.text else ""),
                                       acct.path_level))
            continue

        for t in acct.outs:
            total = acct.traced_net.get(t.group) or acct.traced_total.get(t.group)
            if total and t.amount < total * FOLLOW_SHARE:
                continue   # 가스비·잔돈·수수료 대납
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
                    acct.basis.append(staking_note(acct, ad))
            elif to_kind or (to_lbl.is_contract and ad.stop_at_contract):
                edge["result"] = to_kind or "컨트랙트"
                acct.leads.append(Lead(ad.chain, t.to, t.tx, t.asset, t.amount, None, t.time, acct.address,
                                       f"{edge['result']} 입금" + (f" ({to_lbl.text})" if to_lbl.text else ""),
                                       acct.path_level))
            elif t.to in planned or (hop < hops and len(planned) < max_addresses):
                edge["result"] = "추적"
                if t.to not in planned:
                    planned.add(t.to)
                    queue.append((t.to, hop + 1))
                    pending[t.to] = {"parents": set(), "txs": set(), "expect": [], "level": 0, "address": t.to}
                if t.to not in accounts:   # 아직 조회 전이면 부모 정보를 보탠다
                    q = pending[t.to]
                    q["parents"].add(acct.key)
                    q["txs"].add(t.tx)
                    q["level"] = max(q["level"], acct.path_level)
            else:
                edge["result"] = "리드"
                acct.leads.append(Lead(ad.chain, t.to, t.tx, t.asset, t.amount, None, t.time, acct.address,
                                       f"미추적 {ad.name} 송금 (홉·주소 상한)", acct.path_level))

        acct.deposit_of = deposit_address_of(acct, lbls)
        if acct.deposit_of:
            acct.basis.append(deposit_note(acct))
        # 아직 옮기지 않은 잔액 (추적 자금의 1% 이상, 체인별 최소 금액 이상)
        if ad.balance:
            for g, bal in ad.balance(raw, key).items():
                base = acct.traced_total.get(g) or acct.seed_amount
                if base and bal >= max(base * FOLLOW_SHARE, ad.min_balance_lead.get(g, Decimal(0))):
                    acct.leads.append(Lead(ad.chain, acct.address, "", g, bal, None, raw.get("fetchedAt", 0),
                                           acct.address, "잔액 보유 (조회 시점)", acct.path_level))
    rejudge(accounts, edges, pending, ad)
    # 추적 입금을 못 찾고 멈춘 주소의 리드 금액: 추적 주소들이 그 주소로 보낸 합계
    for acct in accounts.values():
        if acct.stop and not acct.traced_total and acct.leads:
            got = sum((e["amount"] for e in edges if ad.norm(e["to"]) == acct.key), Decimal(0))
            if got:
                acct.leads[0].amount = got
    if len(planned) >= max_addresses:
        log(f"{ad.name} 주소 상한 {max_addresses}개 도달 — 나머지는 리드로 남김")
    return accounts, edges, failed


def rejudge(accounts: dict[str, Account], edges: list[dict], pending: dict[str, dict], ad: Adapter) -> None:
    """BFS 순서상 먼저 판정된 주소에 다른 갈래의 추적 자금이 나중에 들어오면 그 tx를 보태 다시 판정한다.
    이미 멈춘 주소의 유출을 새로 따라가지는 않는다 — 판정·근거·확실도만 갱신. 경로 확실도는 hop 순서대로 다시 전파."""
    for e in edges:
        tgt = accounts.get(ad.norm(e["to"]))
        if e.get("result") == "추적" and tgt and e["tx"] not in tgt.traced_txs:
            tgt.traced_txs.add(e["tx"])
            tgt.parents.add(ad.norm(e["from"]))
    for acct in sorted(accounts.values(), key=lambda a: a.hop):
        seed_level = pending.get(acct.key, {}).get("level", 0) if acct.hop == 0 else 0
        parent_level = max([accounts[p].path_level for p in acct.parents if p in accounts] + [seed_level])
        keep = [b for b in acct.basis if b.endswith("추적 종료") or b.startswith(("주의:", "스테이킹", "스마트 계정"))]
        acct.basis, acct.traced_total, acct.traced_net = [], {}, {}
        acct.swaps_in, acct.returned, acct.staking_in = [], [], []
        judge(acct, parent_level, ad)
        if acct.label.text:
            acct.basis.insert(0, f"태그: {acct.label.text}")
        acct.basis += keep
        if acct.deposit_of:
            acct.basis.append(deposit_note(acct))
        for lead in acct.leads:
            lead.level = acct.path_level
