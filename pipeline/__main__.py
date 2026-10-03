"""사용법 (저장소 루트에서):
    python3 -m pipeline --address bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37 --out out/case2
    python3 -m pipeline --tx 4695e2…8a83 --out out/case1 --hl-hops 0
    python3 -m pipeline --trace hyperliquid-tracer/output/traces/<시작>.json --out out/case2   # 저장된 BTC 추적 재사용
    python3 -m pipeline --hl 0x3A37…6575 --out out/case2-hl                                    # HL 계정부터 시작
    python3 -m pipeline --trace … --out out/case2 --reuse                                      # 받아둔 HL 원자료 재사용
    python3 -m pipeline --start ethereum:0xABC… --out out/x                                     # 아무 체인 주소에서 시작
    python3 -m pipeline --start arbitrum:0x… tron:T… --out out/x                                # 여러 개
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import adapters, blockscout, bridges, core, engine, flow, report, tron


def cached_get(out: Path, reuse: bool):
    """브리지 API 응답을 out/bridge/<요청 해시>.json에 저장하고, reuse면 그걸 다시 쓴다."""
    import hashlib

    def get(url: str) -> dict:
        path = out / "bridge" / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")
        if reuse and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))["data"]
        data = bridges._get(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"url": url, "data": data}, ensure_ascii=False), encoding="utf-8")
        return data
    return get


def cached_fetch(out: Path, chain: str, fetch_address, reuse: bool):
    """주소별 원자료 수집기. 받은 원자료는 out/<체인>/<주소>/raw.json에 저장하고, reuse면 그걸 다시 쓴다."""
    def fetch(addr: str) -> dict:
        path = out / chain / (addr.lower() if addr.startswith("0x") else addr) / "raw.json"
        if reuse and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        raw = fetch_address(addr)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return raw
    return fetch


def chain_fetcher(out: Path, chain: str, reuse: bool):
    """체인별 원자료 수집기 (out/<폴더>/<주소>/raw.json 캐시). Ethereum 폴더 이름은 예전 그대로 eth."""
    if chain == "tron":
        return cached_fetch(out, "tron", tron.fetch_address, reuse)
    return cached_fetch(out, "eth" if chain == "ethereum" else chain,
                        lambda addr: blockscout.fetch_address(addr, chain), reuse)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="크로스체인 자금 흐름 추적 (BTC · Hyperliquid · EVM 체인 · Tron)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--tx", help="BTC 시작 tx (64 hex)")
    src.add_argument("--address", help="BTC 시작 주소")
    src.add_argument("--trace", type=Path, help="hyperliquid-tracer가 저장한 trace JSON")
    src.add_argument("--hl", nargs="+", help="BTC 단계를 건너뛰고 HL 계정부터 시작")
    src.add_argument("--start", nargs="+", metavar="CHAIN:ADDRESS",
                     help=f"아무 체인 주소에서 시작 (체인: {', '.join(adapters.CHAINS)}). 들어온 자금 전체를 추적")
    p.add_argument("--max-depth", type=int, default=7, help="BTC 추적 최대 깊이 (기본 7)")
    p.add_argument("--hl-hops", type=int, default=1, help="HL 계정 간 송금을 따라갈 단계 수 (기본 1, 0이면 안 따라감)")
    p.add_argument("--max-accounts", type=int, default=50, help="조회할 HL 계정 상한 (기본 50)")
    p.add_argument("--eth-hops", type=int, default=2,
                   help="EVM 체인(Ethereum·Arbitrum·Base·Optimism·Polygon)에서 따라갈 단계 수 (기본 2, -1이면 EVM 단계 생략)")
    p.add_argument("--max-eth", type=int, default=30, help="EVM 체인별 조회할 주소 상한 (기본 30)")
    p.add_argument("--no-bridges", action="store_true", help="브리지 목적지(deBridge·LayerZero) 조회 생략")
    p.add_argument("--tron-hops", type=int, default=2, help="Tron 도착 주소에서 따라갈 단계 수 (기본 2, -1이면 생략)")
    p.add_argument("--max-tron", type=int, default=30, help="조회할 Tron 주소 상한 (기본 30)")
    p.add_argument("--out", type=Path, default=Path("out/pipeline"), help="출력 폴더")
    p.add_argument("--reuse", action="store_true", help="out/hl/<계정>/raw.json이 있으면 HL을 다시 조회하지 않음")
    a = p.parse_args(argv)

    if a.tx and not re.fullmatch(r"[0-9a-fA-F]{64}", a.tx):
        p.error("--tx는 64자리 hex")
    if a.hl and not all(core.ADDR.fullmatch(x) for x in a.hl):
        p.error("--hl은 0x + 40자리 주소")
    if a.trace and not a.trace.is_file():
        p.error(f"파일 없음: {a.trace}")
    starts: dict[str, list[str]] = {}
    for item in a.start or []:
        chain, _, addr = item.partition(":")
        if chain not in adapters.CHAINS or not addr:
            p.error(f"--start는 <체인>:<주소> (체인: {', '.join(adapters.CHAINS)}): {item}")
        starts.setdefault(chain, []).append(addr)
    if (a.max_depth < 0 or a.hl_hops < 0 or a.max_accounts < 1 or a.eth_hops < -1 or a.max_eth < 1
            or a.tron_hops < -1 or a.max_tron < 1):
        p.error("깊이·홉은 0 이상 (--eth-hops·--tron-hops는 -1 이상), 계정·주소 상한은 1 이상")

    trace = None
    seeds: dict = {}
    if a.start:
        start = " ".join(a.start) if len(a.start) > 1 else a.start[0]
    elif a.hl:
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

    accounts, edges, failed = {}, [], []
    if seeds:
        accounts, edges, failed = core.hl_stage(seeds, core.raw_fetcher(a.out, a.reuse), a.hl_hops, a.max_accounts,
                                                log=core.log_stderr, token_info=core.hl_api.hl_info)
        core.write_accounts(accounts, a.out)

    # HL 이후 (또는 --start 주소부터): 체인 단계 ↔ 브리지 해석 반복
    hops = {c: (a.tron_hops if c == "tron" else a.eth_hops) for c in adapters.CHAINS}
    maxes = {c: (a.max_tron if c == "tron" else a.max_eth) for c in adapters.CHAINS}
    ads = {c: adapters.adapter_for(c, chain_fetcher(a.out, c, a.reuse)) for c in adapters.CHAINS
           if hops[c] >= 0 or c in starts}
    origin = {c: engine.origin_seeds(addrs, ads[c]) for c, addrs in starts.items()}
    resolve = None if a.no_bridges else (
        lambda todo: bridges.resolve_all(todo, cached_get(a.out, a.reuse), log=core.log_stderr))
    stages = flow.run_chains(accounts, ads, {c: max(h, 0) for c, h in hops.items()}, maxes, resolve, origin,
                             log=core.log_stderr)

    opts = {"max_depth": a.max_depth, "hl_hops": a.hl_hops, "max_accounts": a.max_accounts,
            "eth_hops": a.eth_hops, "max_eth": a.max_eth, "tron_hops": a.tron_hops, "max_tron": a.max_tron}
    path = report.write(a.out, start, trace, accounts, edges, failed, opts, stages)
    leads = report.final_leads(accounts, stages)
    per_chain = ", ".join(f"{r.name} 주소 {len(r.accounts)}개" for r in stages.chains.values()) or "체인 단계 없음"
    core.log_stderr(f"HL 계정 {len(accounts)}개, {per_chain}, 다음 리드 {len(leads)}건 → {path}")
    if failed or any(r.failed for r in stages.chains.values()) or (trace and trace["errors"]):
        return 2
    return 0 if accounts or stages.chains or not (seeds or starts) else 1


if __name__ == "__main__":
    sys.exit(main())
