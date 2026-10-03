"""공개 API 수집기 — Hyperliquid /info, HyperUnit /operations.

인증 없이 누구나 같은 응답을 받는 엔드포인트만 쓴다 (재현성 기준).
"""
from __future__ import annotations

import functools
import http.client
import json
import sys
import time
import urllib.error
import urllib.request

HL_INFO = "https://api.hyperliquid.xyz/info"
UNIT_OPS = "https://api.hyperunit.xyz/operations/{address}"

# HL 시간 범위 조회는 응답당 최대 2000건. 그보다 적게 오면 끝까지 받은 것.
HL_PAGE_LIMIT = 2000
USER_AGENT = "hl-ledger/0.1 (Blockchain at Yonsei team 2)"  # 헤더는 latin-1만 허용


class ApiError(RuntimeError):
    pass


def _request(url: str, body: dict | None = None, retries: int = 8) -> object:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    what = body.get("type") if body else url
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers)
        wait = min(60, 2 ** attempt)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 429/5xx만 재시도. 4xx는 입력 오류라 바로 올린다.
            if e.code != 429 and e.code < 500:
                raise ApiError(f"{what}: HTTP {e.code}") from e
            if e.code == 429:
                # HL 한도는 분당 가중치 1200. 체결 많은 계정은 페이지마다 가중치가 커서 금방 걸린다.
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else max(wait, 10)
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError) as e:
            err = str(getattr(e, "reason", e))
        if attempt == retries - 1:
            break
        print(f"  {what}: {err} — {wait}초 후 재시도 ({attempt + 1}/{retries - 1})", file=sys.stderr)
        time.sleep(wait)
    raise ApiError(f"{what}: {retries}회 시도 모두 실패 ({err})")


def hl_info(body: dict) -> object:
    return _request(HL_INFO, body)


def _paged_by_time(body: dict, key) -> list[dict]:
    """startTime을 밀어가며 전량 수집. 경계 시각이 겹치므로 key로 중복 제거."""
    out: dict = {}
    start = body.get("startTime", 0)
    while True:
        page = hl_info({**body, "startTime": start})
        if not page:
            break
        for row in page:
            out.setdefault(key(row), row)
        last = max(r["time"] for r in page)
        if len(page) < HL_PAGE_LIMIT or last <= start:
            break
        start = last  # 같은 ms에 여러 건이 있을 수 있어 +1 하지 않는다
    return sorted(out.values(), key=lambda r: r["time"])


def ledger_updates(user: str) -> list[dict]:
    """입금·출금·송금 등 펀딩 외 원장 이벤트 전량."""
    return _paged_by_time(
        {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": 0},
        key=lambda r: (r["time"], r["hash"], json.dumps(r["delta"], sort_keys=True)),
    )


def fills(user: str) -> list[dict]:
    """체결 전량. HL은 계정당 최근 10,000건까지만 시간 조회를 허용한다."""
    return _paged_by_time(
        {"type": "userFillsByTime", "user": user, "startTime": 0, "aggregateByTime": False},
        # 먼지 전환(Spot Dust Conversion) 체결은 tid가 겹치므로 oid·time까지 묶는다
        key=lambda r: (r["tid"], r["oid"], r["time"]),
    )


def spot_balances(user: str) -> list[dict]:
    return hl_info({"type": "spotClearinghouseState", "user": user}).get("balances", [])


@functools.lru_cache(maxsize=1)
def spot_pair_names() -> dict[str, str]:
    """'@142' → 'UBTC/USDC' 같은 현물 마켓 이름표."""
    meta = hl_info({"type": "spotMeta"})
    tokens = {t["index"]: t["name"] for t in meta["tokens"]}
    return {u["name"]: "/".join(tokens[i] for i in u["tokens"]) for u in meta["universe"]}


def unit_operations(address: str) -> dict:
    """HyperUnit 입출금 기록. HL 주소·BTC 입금 주소 어느 쪽으로도 조회된다."""
    return _request(UNIT_OPS.format(address=address))


def fetch_all(user: str) -> dict:
    """한 계정의 원자료 묶음. analyze는 이 dict만 보고 동작한다 (오프라인 재분석 가능)."""
    return {
        "user": user,
        "fetchedAt": int(time.time() * 1000),
        "ledger": ledger_updates(user),
        "fills": fills(user),
        "balances": spot_balances(user),
        "unit": unit_operations(user),
        "spotPairs": spot_pair_names(),
    }
