"""Ethereum 공개 API 수집기 — Blockscout v2 (인증 없음, 누구나 같은 응답).

v2 주소 API는 최신순·페이지당 50건이고 주소 오염 스팸이 수천 건 쌓인 주소가 흔하다. 그래서 페이지를 끝까지
(상한까지) 넘겨 이력 전체를 받는다. 토큰은 화이트리스트 컨트랙트별로 필터해서 가짜 토큰 스팸을 피한다.

키 없는 한도: v2는 초당 약 10회. Etherscan 호환 `/api`는 시간당 10회라 쓰지 않는다.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

V2 = "https://eth.blockscout.com/api/v2"
USER_AGENT = "tracker-pipeline/0.1"
MAX_PAGES = 120            # 엔드포인트당 페이지 상한 (×50건). 넘으면 truncated
REQUEST_GAP = 0.12         # 초당 10회 한도 아래로
MAX_RESET_WAIT = 120       # 429 재설정 대기가 이보다 길면 포기 (초)

# 실제 자산으로 인정하는 토큰 (컨트랙트 주소 → 심볼, 소수점). 심볼 "ETH"짜리 가짜 토큰이 흔하다.
WHITELIST = {
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
}


class ApiError(RuntimeError):
    pass


def get(path: str, params: dict | None = None, retries: int = 6) -> dict:
    url = V2 + path + ("?" + urllib.parse.urlencode(params) if params else "")
    err = ""
    for attempt in range(retries):
        time.sleep(REQUEST_GAP)
        wait = min(30, 2 ** attempt)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            if e.code != 429 and e.code < 500:
                raise ApiError(f"{path}: HTTP {e.code}") from e
            err = f"HTTP {e.code}"
            reset = e.headers.get("x-ratelimit-reset")
            if e.code == 429 and reset and reset.isdigit():
                wait = int(reset) / 1000 + 0.5
                if wait > MAX_RESET_WAIT:
                    raise ApiError(f"{path}: 요청 한도 초과, {wait / 60:.0f}분 뒤 재설정") from e
        except (urllib.error.URLError, TimeoutError) as e:
            err = str(getattr(e, "reason", e))
        print(f"  blockscout {path}: {err} — {wait:.1f}초 후 재시도", file=sys.stderr)
        time.sleep(wait)
    raise ApiError(f"{path}: {retries}회 시도 모두 실패 ({err})")


def sweep(path: str, params: dict | None = None, max_pages: int = MAX_PAGES) -> tuple[list[dict], bool]:
    """최신순 페이지를 끝까지. 돌려주는 bool은 '상한에 걸려 더 오래된 기록이 남았다'."""
    items, nxt = [], None
    for _ in range(max_pages):
        page = get(path, {**(params or {}), **(nxt or {})})
        items += page.get("items", [])
        nxt = page.get("next_page_params")
        if not nxt:
            return items, False
    return items, True


def transaction(tx: str) -> dict:
    return get(f"/transactions/{tx}")


def fetch_address(address: str) -> dict:
    """한 주소의 원자료 묶음 (eth.judge는 이 dict만 보고 동작 — 오프라인 재분석 가능)."""
    raw = {"address": address, "fetchedAt": int(time.time() * 1000), "info": get(f"/addresses/{address}")}
    truncated = False
    raw["txs"], cut = sweep(f"/addresses/{address}/transactions")
    truncated |= cut
    raw["internal"], cut = sweep(f"/addresses/{address}/internal-transactions")
    truncated |= cut
    raw["tokens"] = []
    for contract in WHITELIST:
        rows, cut = sweep(f"/addresses/{address}/token-transfers", {"type": "ERC-20", "token": contract})
        raw["tokens"] += rows
        truncated |= cut
    raw["truncated"] = truncated
    return raw
