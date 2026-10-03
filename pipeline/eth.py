"""Ethereum 구간 추적 — 체인 공통 엔진(engine.py) + EVM 어댑터(adapters.py)의 얇은 래퍼.

추적·판정 로직은 engine.py에 있다. 이 모듈은 예전 이름(eth_stage, seeds_from_leads …)을 유지한다.
"""
from __future__ import annotations

from typing import Callable

from . import engine
from .adapters import evm_adapter, evm_labels as labels, evm_transfers as transfers   # noqa: F401
from .core import LEVEL_NAME, Lead   # noqa: F401
from .engine import (DEPOSIT_SHARE, DEPOSIT_WINDOW_MS, FOLLOW_SHARE, TERMINAL, Account, Label,   # noqa: F401
                     Transfer, fmt, lookalike)

EthAccount = Account


def seeds_from_leads(leads: list[Lead]) -> dict[str, dict]:
    """HL → Ethereum 출금 리드를 목적지 주소별로 묶는다."""
    return engine.seeds_from_leads(leads, evm_adapter("ethereum"))


def eth_stage(seeds: dict[str, dict], fetch: Callable[[str], dict], hops: int = 2, max_addresses: int = 30,
              log=print) -> tuple[dict[str, Account], list[dict], list[tuple[str, str]]]:
    """HL 출금 주소에서 시작해 ETH 송금을 hops단계까지 따라간다. 돌려주는 것: 주소들, 간선, 수집 실패."""
    return engine.stage(seeds, evm_adapter("ethereum", fetch), hops, max_addresses, log)
