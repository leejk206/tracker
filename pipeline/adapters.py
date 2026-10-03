"""체인 어댑터: 체인별 원자료 → 엔진이 쓰는 Transfer·Label·규칙.

- EVM (Blockscout v2): ethereum · arbitrum · base · optimism · polygon — blockscout.EVM_CHAINS 설정 한 줄로 추가
- Tron (TronScan): USDT(TRC-20)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Callable

from . import blockscout, tron
from .engine import Adapter, Label, Transfer

# 혼입 비율을 비교하는 묶음
GROUP = {"ETH": "ETH", "WETH": "ETH", "POL": "POL", "WPOL": "POL",
         "USDC": "USD", "USDC.e": "USD", "USDT": "USD", "DAI": "USD"}
EVM_DUST = {"ETH": Decimal("0.001"), "POL": Decimal("1"), "USD": Decimal("1")}
# 오염 주소가 보내는 금액은 먼지 수준. 닮은 주소라도 이보다 크면 실제 거래로 본다.
EVM_POISON_MAX = {"ETH": Decimal("0.1"), "POL": Decimal("100"), "USD": Decimal("100")}
EVM_MIN_BALANCE = {"ETH": Decimal("0.1"), "POL": Decimal("100")}   # 이보다 적은 잔액은 가스비 잔돈
# 그 외 유입을 설명할 때 이름을 붙이는 서비스 출금 주소
SERVICE_SENDERS = {
    "ethereum": {"0xbea9f7fd27f4ee20066f18def0bc586ec221055a": "HyperUnit",    # Unit: Treasury
                 "0x4bbe9b84aac9804557e8a90b7186324f20357e5c": "HyperUnit"},   # Treasury가 호출하는 배치 컨트랙트(태그 없음)
    "arbitrum": {"0x2df1c51e09aecf9cacb7bc98cb1742757f163df7": "Hyperliquid"},  # HL Bridge2 (USDC 입출금)
}


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _a(o: dict | None) -> str:
    return ((o or {}).get("hash") or "").lower()


def to_label(o: dict | None) -> Label:
    if not o:
        return Label()
    tags = [t["name"].strip() for t in ((o.get("metadata") or {}).get("tags") or [])
            if t.get("tagType") == "name" and t.get("name")]
    tags += [t["display_name"] for t in o.get("public_tags") or [] if t.get("display_name")]
    return Label(o.get("name") or "", tags, bool(o.get("is_contract")))


def evm_labels(raw: dict) -> dict[str, Label]:
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


def evm_transfers(raw: dict) -> list[Transfer]:
    """v2 원자료 → 실제 자산 이동만 (실패·0원·화이트리스트 밖 토큰 제외). 체인은 raw["chain"] (기본 ethereum)."""
    cfg = blockscout.EVM_CHAINS[raw.get("chain", "ethereum")]
    native = cfg["native"]
    out: dict[tuple, Transfer] = {}

    def add(key, t: Transfer):
        t.group = GROUP.get(t.asset, t.asset)
        out[key] = t

    for t in raw.get("txs", []):
        v = int(t.get("value") or 0)
        if v > 0 and t.get("result") == "success" and t.get("to"):
            add((t["hash"].lower(), "tx"), Transfer(_ms(t["timestamp"]), int(t["block_number"]), t["hash"].lower(),
                                                    _a(t["from"]), _a(t["to"]), native, Decimal(v) / 10 ** 18, "tx"))
    for t in raw.get("internal", []):
        v = int(t.get("value") or 0)
        if v > 0 and t.get("success") and t.get("to"):
            tx = t["transaction_hash"].lower()
            add((tx, "internal", t.get("index")), Transfer(_ms(t["timestamp"]), int(t["block_number"]), tx,
                                                           _a(t["from"]), _a(t["to"]), native, Decimal(v) / 10 ** 18,
                                                           "internal"))
    for t in raw.get("tokens", []):
        tok = cfg["tokens"].get(((t.get("token") or {}).get("address_hash") or "").lower())
        if not tok or (t.get("total") or {}).get("value") is None:
            continue
        sym, dec = tok
        tx = t["transaction_hash"].lower()
        add((tx, "token", t.get("log_index")), Transfer(_ms(t["timestamp"]), int(t["block_number"]), tx,
                                                        _a(t["from"]), _a(t["to"]), sym,
                                                        Decimal(t["total"]["value"]) / Decimal(10) ** dec, "token"))
    for w in raw.get("withdrawals", []):   # 비콘 체인 출금 (스테이킹 회수·보상) — Ethereum
        v = int(w.get("amount") or 0)
        if v > 0:
            add(("withdrawal", w.get("index")), Transfer(_ms(w["timestamp"]), int(w["block_number"]),
                                                         f"withdrawal:{w.get('index')}", "beacon",
                                                         _a(w.get("receiver")) or raw["address"].lower(),
                                                         native, Decimal(v) / 10 ** 18, "withdrawal"))
    return sorted(out.values(), key=lambda t: (t.block, t.time))


def evm_balance(raw: dict, key: str) -> dict[str, Decimal]:
    native = blockscout.EVM_CHAINS[raw.get("chain", "ethereum")]["native"]
    bal = Decimal(int((raw.get("info") or {}).get("coin_balance") or 0)) / 10 ** 18
    return {GROUP.get(native, native): bal}


def evm_adapter(chain: str, fetch: Callable[[str], dict] | None = None) -> Adapter:
    cfg = blockscout.EVM_CHAINS[chain]

    def tagged(raw: dict) -> dict:
        raw.setdefault("chain", chain)   # 예전 원자료·테스트 원자료에는 체인 표시가 없다
        return raw

    return Adapter(
        chain=chain, name=cfg["name"],
        fetch=fetch or (lambda a: blockscout.fetch_address(a, chain)),
        transfers=lambda raw: evm_transfers(tagged(raw)),
        labels=evm_labels, dust=EVM_DUST, poison_max=EVM_POISON_MAX,
        display=lambda raw, key: (raw.get("info") or {}).get("hash") or key,
        balance=lambda raw, key: evm_balance(tagged(raw), key), min_balance_lead=EVM_MIN_BALANCE,
        service_senders=SERVICE_SENDERS.get(chain, {}), errors=(blockscout.ApiError,),
    )


# ── Tron ──────────────────────────────────────────────────────────────────────

def tron_transfers(raw: dict) -> list[Transfer]:
    out: dict[tuple, Transfer] = {}
    for t in raw.get("transfers", []):
        tok = tron.TOKENS.get(t.get("contract_address", ""))
        if not tok or t.get("contractRet", "SUCCESS") != "SUCCESS" or t.get("revert"):
            continue
        ts = int(t["block_ts"])
        out[(t["transaction_id"], t["from_address"], t["to_address"], t["quant"])] = Transfer(
            ts, ts, t["transaction_id"].lower().removeprefix("0x"), t["from_address"], t["to_address"], tok[0],
            Decimal(t["quant"]) / Decimal(10) ** tok[1], "token", GROUP.get(tok[0], tok[0]))
    return sorted(out.values(), key=lambda t: t.time)


def tron_labels(raw: dict) -> dict[str, Label]:
    tags, contracts = raw.get("tags", {}), set(raw.get("contracts", []))
    return {a: Label("", [tags[a]] if tags.get(a) else [], a in contracts) for a in set(tags) | contracts}


def tron_balance(raw: dict, key: str) -> dict[str, Decimal]:
    if raw.get("truncated"):
        return {}
    ts = tron_transfers(raw)
    return {"USD": sum((t.amount for t in ts if t.to == key), Decimal(0)) - sum((t.amount for t in ts if t.frm == key), Decimal(0))}


def tron_parse(addr: str) -> str:
    """hex(0x41… 또는 0x…) → base58. 이미 base58이면 그대로."""
    if addr.startswith("0x") and len(addr) in (42, 44):
        from .bridges import chain_address
        return chain_address("tron", bytes.fromhex(addr[2:]).rjust(32, b"\0"))
    return addr


def tron_adapter(fetch: Callable[[str], dict] | None = None) -> Adapter:
    return Adapter(
        chain="tron", name="Tron", fetch=fetch or tron.fetch_address, transfers=tron_transfers, labels=tron_labels,
        dust={"USD": Decimal("1")}, norm=lambda a: a, parse=tron_parse,
        stop_at_contract=False,                  # 수수료 대납(GasFree 등) 스마트 계정도 컨트랙트로 나온다
        mixed_stop_ratio=tron.MIXED_STOP_RATIO,  # 남의 자금이 더 많은 지갑의 유출은 따라가지 않는다
        balance=tron_balance, errors=(tron.ApiError,),
    )


# ── 등록부 ────────────────────────────────────────────────────────────────────

CHAINS = list(blockscout.EVM_CHAINS) + ["tron"]   # 추적 단계를 돌릴 수 있는 체인


def adapter_for(chain: str, fetch: Callable[[str], dict] | None = None) -> Adapter:
    if chain == "tron":
        return tron_adapter(fetch)
    return evm_adapter(chain, fetch)
