"""체인을 넘나드는 반복: 리드의 체인에 어댑터가 있으면 그 체인 단계를 돌리고, 브리지를 해석해 새 체인으로 넘어간다.

HL 단계(또는 --start 시작 주소) → [체인 단계들 → 브리지 해석] × 최대 MAX_ROUNDS
라운드마다 아직 추적하지 않은 목적지만 새로 돌리므로, 같은 주소를 두 번 조회하지 않는다.
"""
from __future__ import annotations

from typing import Callable

from . import bridges, engine
from .engine import Adapter
from .report import ChainResult, Stages, final_leads

MAX_ROUNDS = 5


def run_chains(hl_accounts: dict, adapters: dict[str, Adapter], hops: dict[str, int], maxes: dict[str, int],
               resolve: Callable[[list], dict] | None = None, origin: dict[str, dict] | None = None,
               log=print) -> Stages:
    """체인 단계들을 반복 실행한다.

    adapters: 돌릴 체인 → 어댑터 (hops가 음수인 체인은 넣지 않는다)
    resolve:  브리지 입금 리드 목록 → {출발 tx: [BridgeHop]} (None이면 브리지 해석 안 함)
    origin:   체인 → 시작 주소 seeds (--start)
    """
    st = Stages()
    for rnd in range(MAX_ROUNDS):
        progressed = False
        leads = final_leads(hl_accounts, st)
        for chain, ad in adapters.items():
            r = st.chains.get(chain)
            # 다른 단계(HL·다른 체인·브리지)에서 넘어온 리드만 시작점. 이 체인 단계가 스스로 남긴 리드
            # (홉 상한에 걸린 송금, 거래소·컨트랙트 입금 등)를 다시 시작점으로 잡으면 라운드마다 추적이 끝없이 넓어진다.
            incoming = [l for l in leads if not (r and r.has(l.source))]
            seeds = engine.seeds_from_leads(incoming, ad)
            if rnd == 0 and origin and chain in origin:
                seeds.update(origin[chain])
            seeds = {k: v for k, v in seeds.items() if not (r and k in r.accounts)}
            if not seeds:
                continue
            log(f"[{ad.name}] 추적 시작 주소 {len(seeds)}개 (라운드 {rnd + 1})")
            accounts, edges, failed = engine.stage(seeds, ad, hops.get(chain, 2), maxes.get(chain, 30), log)
            r = st.chains.setdefault(chain, ChainResult(chain, ad.name, ad.norm))
            r.accounts.update(accounts)
            r.edges += edges
            r.failed += failed
            progressed = True
            leads = final_leads(hl_accounts, st)
        if resolve:
            todo = [l for l in final_leads(hl_accounts, st) if bridges.is_bridge_lead(l) and l.tx not in st.bridge_hops]
            if todo:
                st.bridge_hops.update(resolve(todo))
                progressed = True
        if not progressed:
            break
    return st
