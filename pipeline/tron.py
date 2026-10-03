"""Tron 원자료 수집 (TronScan 공개 API, 키 없음) + 추적 단계 래퍼.

추적·판정 로직은 체인 공통 엔진(engine.py), Tron 규칙은 adapters.tron_adapter에 있다.
페이지 상한(2,000건)을 넘는 주소는 추적 입금을 못 찾으면 허브로 보고 멈춘다.
키 없이는 주소 태그가 거의 비어 있어 거래소 판별은 태그가 있을 때만 한다.
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Callable

from .core import LEVEL_NAME, Lead   # noqa: F401

API = "https://apilist.tronscanapi.com/api"
USER_AGENT = "tracker-pipeline/0.1"
TOKENS = {"TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t": ("USDT", 6)}   # Tron USDT (TRC-20)
PAGE = 50
MAX_PAGES = 40             # 주소당 최대 2,000건. 넘으면 truncated (오염 스팸이 많은 주소도 추적 입금까지는 닿게)
REQUEST_GAP = 0.3
# 그 외 유입이 추적 자금 이상이면(추적 자금이 절반 이하) 남의 자금이 더 많은 지갑 — 유출을 따라가면 리드가 남의 돈으로 불어난다
MIXED_STOP_RATIO = Decimal(1)


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
    엔진은 이 dict만 보고 동작한다 (오프라인 재분석 가능).
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


def seeds_from_leads(leads: list[Lead]) -> dict[str, dict]:
    """브리지 도착 리드(chain=tron)를 받는 주소별로 묶는다."""
    from . import engine
    from .adapters import tron_adapter
    return engine.seeds_from_leads(leads, tron_adapter())


def tron_stage(seeds: dict[str, dict], fetch: Callable[[str], dict], hops: int = 2, max_addresses: int = 30,
               log=print):
    """브리지 도착 주소에서 시작해 USDT 송금을 hops단계까지 따라간다. 돌려주는 것: 주소들, 간선, 수집 실패."""
    from . import engine
    from .adapters import tron_adapter
    return engine.stage(seeds, tron_adapter(fetch), hops, max_addresses, log)
