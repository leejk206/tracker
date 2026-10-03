"""BTC → HyperUnit → Hyperliquid → 다음 체인까지 잇는 전체 파이프라인.

두 모듈을 그대로 가져다 쓴다 (코드 복사 없음):
- hyperliquid-tracer/ : BTC UTXO 소비 관계 BFS + HyperUnit 매칭 → HL 계정
- hl_ledger/          : HL 계정 원장·체결 → 입출금 표 + 1:1 판정
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("hyperliquid-tracer", "hl_ledger"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
