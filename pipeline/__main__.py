"""사용법 (저장소 루트에서):
    python3 -m pipeline --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37 --out out/case2
    python3 -m pipeline --tx 4695e2…8a83 --out out/case1 --hl-hops 0
    python3 -m pipeline --trace hyperliquid-tracer/output/traces/<시작>.json --out out/case2   # 저장된 BTC 추적 재사용
    python3 -m pipeline --hl 0x3A37…6575 --out out/case2-hl                                    # HL 계정부터 시작
    python3 -m pipeline --trace … --out out/case2 --reuse                                      # 받아둔 HL 원자료 재사용
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import core, report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="BTC → HyperUnit → Hyperliquid → 다음 체인 자금 흐름 추적")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--tx", help="BTC 시작 tx (64 hex)")
    src.add_argument("--address", help="BTC 시작 주소")
    src.add_argument("--trace", type=Path, help="hyperliquid-tracer가 저장한 trace JSON")
    src.add_argument("--hl", nargs="+", help="BTC 단계를 건너뛰고 HL 계정부터 시작")
    p.add_argument("--max-depth", type=int, default=7, help="BTC 추적 최대 깊이 (기본 7)")
    p.add_argument("--hl-hops", type=int, default=1, help="HL 계정 간 송금을 따라갈 단계 수 (기본 1, 0이면 안 따라감)")
    p.add_argument("--max-accounts", type=int, default=50, help="조회할 HL 계정 상한 (기본 50)")
    p.add_argument("--out", type=Path, default=Path("out/pipeline"), help="출력 폴더")
    p.add_argument("--reuse", action="store_true", help="out/hl/<계정>/raw.json이 있으면 HL을 다시 조회하지 않음")
    a = p.parse_args(argv)

    if a.tx and not re.fullmatch(r"[0-9a-fA-F]{64}", a.tx):
        p.error("--tx는 64자리 hex")
    if a.hl and not all(core.ADDR.fullmatch(x) for x in a.hl):
        p.error("--hl은 0x + 40자리 주소")
    if a.trace and not a.trace.is_file():
        p.error(f"파일 없음: {a.trace}")
    if a.max_depth < 0 or a.hl_hops < 0 or a.max_accounts < 1:
        p.error("깊이·홉은 0 이상, 계정 상한은 1 이상")

    trace = None
    if a.hl:
        start = a.hl[0] if len(a.hl) == 1 else f"HL 계정 {len(a.hl)}개"
        seeds = {x.lower(): None for x in a.hl}
    else:
        if a.trace:
            trace = json.loads(a.trace.read_text(encoding="utf-8-sig"))
        else:
            core.log_stderr("[BTC] UTXO 추적 중… (공개 API, 수 분 걸릴 수 있음)")
            from clients.http import APIError
            try:
                trace = core.btc_trace(a.tx, a.address, a.max_depth)
            except APIError as e:
                core.log_stderr(f"BTC 시작 조회 실패: {e}")
                return 1
        start = trace["start"]
        seeds = core.seeds_from_trace(trace)
        core.log_stderr(f"[BTC] output {len(trace['nodes'])}개, 매칭 {len(trace['matches'])}건 → HL 계정 {len(seeds)}개")
        if not seeds:
            core.log_stderr("HyperUnit으로 들어간 HL 계정을 찾지 못함 (깊이를 늘리거나 시작점을 바꿔볼 것)")

    accounts, edges, failed = core.hl_stage(seeds, core.raw_fetcher(a.out, a.reuse), a.hl_hops, a.max_accounts,
                                            log=core.log_stderr)
    core.write_accounts(accounts, a.out)
    opts = {"max_depth": a.max_depth, "hl_hops": a.hl_hops, "max_accounts": a.max_accounts}
    path = report.write(a.out, start, trace, accounts, edges, failed, opts)
    leads = report.all_leads(accounts)
    core.log_stderr(f"HL 계정 {len(accounts)}개, 송금 {len(edges)}건, 다음 체인 리드 {len(leads)}건 → {path}")
    if failed or (trace and trace["errors"]):
        return 2
    return 0 if accounts or not seeds else 1


if __name__ == "__main__":
    sys.exit(main())
