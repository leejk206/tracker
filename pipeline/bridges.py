"""브리지 목적지 해석: Ethereum 브리지 입금 tx → 목적 체인 · 받는 주소 · 도착 금액 · 도착 tx.

공개 API (인증 없음):
- deBridge (DLN): stats-api.dln.trade — tx → orderId → 주문 상세 (목적 체인 chainId, receiverDst, takeOffer, 도착 tx)
- LayerZero (USDT0 등 OFT): scan.layerzero-api.com — tx → 메시지 (목적 체인, 도착 tx, payload)
  OFT payload 끝은 [받는 주소 bytes32][금액 uint64, sharedDecimals 6].
"""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .core import Lead

DLN = "https://stats-api.dln.trade/api"
LZ = "https://scan.layerzero-api.com/v1"
USER_AGENT = "tracker-pipeline/0.1"

# deBridge 내부 chainId → 체인 이름
DLN_CHAINS = {1: "ethereum", 10: "optimism", 56: "bsc", 137: "polygon", 8453: "base", 42161: "arbitrum",
              43114: "avalanche", 59144: "linea", 7565164: "solana", 100000001: "neon", 100000002: "gnosis",
              100000004: "metis", 100000005: "bitrock", 100000014: "sonic", 100000017: "abstract",
              100000020: "berachain", 100000022: "hyperevm", 100000026: "tron"}
DEBRIDGE_CONTRACTS = {"0x663dc15d3c1ac63ff12e45ab68fea3f0a883c251", "0xef4fb24ad0916217251f553c0596f8edc630eb66"}
OFT_SHARED_DECIMALS = 6
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class ApiError(RuntimeError):
    pass


def _get(url: str, retries: int = 5) -> dict:
    err = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code != 429 and e.code < 500:
                raise ApiError(f"{url}: HTTP {e.code}") from e
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError, json.JSONDecodeError) as e:
            err = str(getattr(e, "reason", e))
        wait = min(30, 2 ** attempt)
        print(f"  bridge API: {err} — {wait}초 후 재시도", file=sys.stderr)
        time.sleep(wait)
    raise ApiError(f"{url}: {retries}회 시도 모두 실패 ({err})")


def b58check(payload: bytes) -> str:
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58(data: bytes) -> str:
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def chain_address(chain: str, raw32: bytes) -> str:
    """bytes32 주소 → 체인 표기. Tron은 0x41 + 20바이트 base58check, Solana는 32바이트 base58, 그 외 EVM 0x."""
    if chain == "tron":
        return b58check(b"\x41" + raw32[-20:])
    if chain == "solana":
        return b58(raw32)
    return "0x" + raw32[-20:].hex()


@dataclass
class BridgeHop:
    protocol: str
    src_tx: str
    src_address: str     # 브리지에 넣은 Ethereum 주소
    dst_chain: str
    receiver: str
    token: str
    amount: Decimal | None
    status: str
    dst_tx: str
    time: int            # 도착 시각 (ms), 모르면 출발 시각


def resolve_debridge(tx: str, get=_get) -> list[BridgeHop]:
    ids = (get(f"{DLN}/Transaction/{tx}/orderIds") or {}).get("orderIds") or []
    hops = []
    for oid in ids:
        o = get(f"{DLN}/Orders/{oid['stringValue']}")
        if not o:
            continue
        take = o.get("takeOfferWithMetadata") or {}
        chain_id = int((take.get("chainId") or {}).get("bigIntegerValue") or 0)
        chain = DLN_CHAINS.get(chain_id, f"chain {chain_id}")
        dec = int(take.get("decimals") or (take.get("metadata") or {}).get("decimals") or 0)
        amt = (o.get("actualFulfillAmount") or take.get("amount") or {}).get("bigIntegerValue")
        dst = o.get("fulfilledDstEventMetadata") or {}
        src = o.get("createdSrcEventMetadata") or {}
        state = o.get("state") or "?"
        if "cancel" in state.lower():
            # 취소된 주문: 목적 체인에 도착하지 않고 출발 체인으로 환불된다
            give = o.get("giveOfferWithMetadata") or {}
            gdec = int(give.get("decimals") or (give.get("metadata") or {}).get("decimals") or 0)
            gamt = (give.get("amount") or {}).get("bigIntegerValue")
            refund = (o.get("claimedOrderCancelSrcEventInfo") or {}).get("transactionMetadata") or {}
            hops.append(BridgeHop(
                "deBridge 취소·환불", tx, (o.get("makerSrc") or {}).get("stringValue", ""), "ethereum",
                (o.get("cancelBeneficiarySrc") or o.get("makerSrc") or {}).get("stringValue", ""),
                give.get("symbol") or "?", Decimal(gamt) / Decimal(10) ** gdec if gamt is not None else None, state,
                (refund.get("transactionHash") or {}).get("stringValue") or "",
                int(refund.get("blockTimeStamp") or src.get("blockTimeStamp") or 0) * 1000))
            continue
        hops.append(BridgeHop(
            "deBridge", tx, (o.get("makerSrc") or {}).get("stringValue", ""), chain,
            (o.get("receiverDst") or {}).get("stringValue", ""), take.get("symbol") or "?",
            Decimal(amt) / Decimal(10) ** dec if amt is not None else None, o.get("state") or "?",
            (dst.get("transactionHash") or {}).get("stringValue") or "",
            int(dst.get("blockTimeStamp") or src.get("blockTimeStamp") or 0) * 1000))
    return hops


def resolve_layerzero(tx: str, get=_get) -> list[BridgeHop]:
    hops = []
    for m in (get(f"{LZ}/messages/tx/{tx}") or {}).get("data") or []:
        path, src, dst = m.get("pathway") or {}, (m.get("source") or {}).get("tx") or {}, m.get("destination") or {}
        chain = (path.get("receiver") or {}).get("chain") or f"eid {path.get('dstEid')}"
        payload = bytes.fromhex((src.get("payload") or "0x")[2:])
        receiver, amount = "", None
        if len(payload) >= 40:   # OFT 메시지: … [to bytes32][amountSD uint64]
            receiver = chain_address(chain, payload[-40:-8])
            amount = Decimal(int.from_bytes(payload[-8:], "big")) / Decimal(10) ** OFT_SHARED_DECIMALS
        name = (path.get("sender") or {}).get("name") or "LayerZero"
        dtx = dst.get("tx") or {}
        hops.append(BridgeHop(f"{name} (LayerZero)", tx, src.get("from", ""), chain, receiver,
                              name if name.upper().startswith("USD") else "?", amount,
                              dst.get("status") or (m.get("status") or {}).get("name") or "?",
                              dtx.get("txHash", ""), int(dtx.get("blockTimestamp") or src.get("blockTimestamp") or 0) * 1000))
    return hops


def resolve(lead: Lead, get=_get) -> list[BridgeHop]:
    """브리지 입금 리드 하나 → 도착 정보. 어느 프로토콜인지는 컨트랙트·태그로 고른다."""
    kind = lead.kind.lower()
    if lead.address.lower() in DEBRIDGE_CONTRACTS or "debridge" in kind:
        return resolve_debridge(lead.tx, get)
    if "oft" in kind or "usdt0" in kind or "layerzero" in kind or "stargate" in kind:
        return resolve_layerzero(lead.tx, get)
    return []


def is_bridge_lead(l: Lead) -> bool:
    return l.chain == "ethereum" and l.kind.startswith("브리지 입금") and bool(l.tx)


def resolve_all(leads: list[Lead], get: Callable[[str], dict] = _get, log=print) -> dict[str, list[BridgeHop]]:
    """브리지 입금 리드들 → {src tx: 도착 정보}. 같은 tx는 한 번만 조회."""
    out: dict[str, list[BridgeHop]] = {}
    targets = [l for l in leads if is_bridge_lead(l)]
    for i, l in enumerate(targets, 1):
        if l.tx in out:
            continue
        log(f"[BRIDGE] {i}/{len(targets)} {l.tx[:12]}… 목적지 조회")
        try:
            out[l.tx] = resolve(l, get)
        except ApiError as e:
            log(f"  실패: {e}")
            out[l.tx] = []
    return out


def arrival_leads(leads: list[Lead], hops: dict[str, list[BridgeHop]]) -> list[Lead]:
    """브리지 입금 리드를 도착 리드(목적 체인·받는 주소)로 바꾼다. 해석 못 한 것은 그대로 둔다."""
    out = []
    for l in leads:
        got = hops.get(l.tx) if is_bridge_lead(l) else None
        if not got:
            out.append(l)
            continue
        for h in got:
            out.append(Lead(h.dst_chain, h.receiver, h.dst_tx, h.token, h.amount if h.amount is not None else Decimal(0),
                            None, h.time or l.time, l.source, f"{h.protocol} 도착 ({h.status})", l.level))
    return sorted(out, key=lambda x: x.time)
