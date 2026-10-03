"""파이프라인 결과 → pipeline.md (흐름 요약·다이어그램) / pipeline.json / leads.csv."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from .core import LEVEL_NAME, Account, Lead, is_hyperevm_bridge
from hl_ledger.analyze import fmt, ts
from hl_ledger.report import EXPLORER, HL_TX, md_link, short, table

EXPLORER_ADDR = {
    "bitcoin": "https://mempool.space/address/{}",
    "ethereum": "https://etherscan.io/address/{}",
    "solana": "https://solscan.io/account/{}",
    "arbitrum": "https://arbiscan.io/address/{}",
    "hyperliquid": "https://app.hyperliquid.xyz/explorer/address/{}",
    "hyperevm": "https://hyperevmscan.io/address/{}",
}
MERMAID_MAX_ACCOUNTS = 30
MERMAID_MAX_ETH = 20
ETH_TX = "https://etherscan.io/tx/{}"


def _usd(x) -> str:
    return "-" if x is None else fmt(x, 2)


def _addr(chain: str, a: str) -> str:
    tpl = EXPLORER_ADDR.get(chain)
    return md_link(f"`{short(a)}`", tpl.format(a) if tpl else None)


def _tx(chain: str, tx: str) -> str:
    if not tx:
        return "-"
    tpl = EXPLORER.get(chain) or (HL_TX if chain.startswith("hyper") or chain == "arbitrum" else None)
    return md_link(short(tx, 8), tpl.format(tx) if tpl else None)


def all_leads(accounts: dict[str, Account]) -> list[Lead]:
    return sorted((l for a in accounts.values() for l in a.leads), key=lambda l: l.time)


def final_leads(accounts: dict[str, Account], eth_accounts: dict | None = None) -> list[Lead]:
    """최종 리드: HL 리드 중 ETH 단계가 이어받은 출금은 빼고, ETH 단계의 종착·미추적 리드를 더한다."""
    eth_accounts = eth_accounts or {}
    hl = [l for l in all_leads(accounts) if not (l.chain == "ethereum" and l.address.lower() in eth_accounts)]
    eth_leads = [l for a in eth_accounts.values() for l in a.leads]
    return sorted(hl + eth_leads, key=lambda l: l.time)


def eth_mermaid(eth_accounts: dict, hl_ids: dict[str, str]) -> list[str]:
    """ETH 주소 노드·간선·종착 묶음. HL 계정 노드(hl_ids)에서 이어 그린다."""
    shown = sorted(eth_accounts.values(), key=lambda a: (a.hop, a.key))[:MERMAID_MAX_ETH]
    ids = {a.key: f"E{i}" for i, a in enumerate(shown)}
    lines = []
    for a in shown:
        tag = f"<br>{a.label.text[:30]}" if a.label.text else ""
        stop = f" · {a.stop}" if a.stop else ""
        lines.append(f'  {ids[a.key]}["ETH {short(a.address)}<br>hop {a.hop} · {LEVEL_NAME[a.path_level]}{stop}{tag}"]')
        for par in sorted(a.parents):
            if par in hl_ids:
                lines.append(f"  {hl_ids[par]} -->|Unit 출금| {ids[a.key]}")
            elif par in ids:
                lines.append(f"  {ids[par]} --> {ids[a.key]}")
        groups: dict[str, list[Lead]] = defaultdict(list)
        for l in a.leads:
            groups[f"{l.kind.split(' (')[0]}|{l.token}"].append(l)
        for n, (k, ls) in enumerate(groups.items()):
            kind, token = k.split("|")
            amt = sum((l.amount for l in ls), Decimal(0))
            lines.append(f'  {ids[a.key]}L{n}(["{kind}<br>{len(ls)}건 {fmt(amt)} {token}"])')
            lines.append(f"  {ids[a.key]} --> {ids[a.key]}L{n}")
    if len(eth_accounts) > len(shown):
        lines.append(f'  emore["외 ETH 주소 {len(eth_accounts) - len(shown)}개 (표 참고)"]')
    return lines


def mermaid(start: str, accounts: dict[str, Account], eth_accounts: dict | None = None) -> str:
    """시작점 → HL 계정(홉) → 체인별 리드 묶음. 계정이 많으면 상위만 그린다."""
    shown = sorted(accounts.values(), key=lambda a: (a.hop, -sum((l.usdc_value or 0) for l in a.leads)))
    shown = shown[:MERMAID_MAX_ACCOUNTS]
    ids = {a.key: f"A{i}" for i, a in enumerate(shown)}
    lines = ["```mermaid", "flowchart LR"]
    lines.append(f'  S["시작<br>{short(start)}"]')
    for a in shown:
        hub = " · 허브" if a.hub else ""
        lines.append(f'  {ids[a.key]}["HL {short(a.address)}<br>hop {a.hop} · {LEVEL_NAME[a.path_level]}{hub}"]')
        if a.hop == 0:
            label = f"BTC {len(a.btc['matches'])}건" if a.btc else "입력"
            lines.append(f"  S -->|{label}| {ids[a.key]}")
        for p in sorted(a.parents):
            if p in ids:
                lines.append(f"  {ids[p]} -->|HL 송금| {ids[a.key]}")
        groups: dict[tuple[str, str], list[Lead]] = defaultdict(list)
        for l in a.leads:
            if l.chain == "ethereum" and l.address.lower() in (eth_accounts or {}):
                continue   # ETH 노드로 따로 그린다
            groups[(l.chain, l.token)].append(l)
        for n, ((chain, token), ls) in enumerate(groups.items()):
            amt = sum((l.amount for l in ls), Decimal(0))
            lines.append(f'  {ids[a.key]}L{n}(["{chain}<br>{len(ls)}건 {fmt(amt)} {token}"])')
            lines.append(f"  {ids[a.key]} --> {ids[a.key]}L{n}")
    if len(accounts) > len(shown):
        lines.append(f'  more["외 계정 {len(accounts) - len(shown)}개 (표 참고)"]')
    if eth_accounts:
        lines += eth_mermaid(eth_accounts, ids)
    lines.append("```")
    return "\n".join(lines)


def render(start: str, trace: dict | None, accounts: dict[str, Account], edges: list[dict],
           failed: list[tuple[str, str]], opts: dict, eth_accounts: dict | None = None,
           eth_edges: list[dict] | None = None, eth_failed: list[tuple[str, str]] | None = None) -> str:
    eth_accounts, eth_edges, eth_failed = eth_accounts or {}, eth_edges or [], eth_failed or []
    leads = final_leads(accounts, eth_accounts)
    hop_count = Counter(a.hop for a in accounts.values())
    by_chain = Counter(l.chain for l in leads)
    parts = [f"# 자금 흐름 추적 — `{start}`", ""]

    summary = []
    if trace:
        summary.append(f"BTC 추적: output {len(trace['nodes'])}개, HyperUnit 매칭 {len(trace['matches'])}건, "
                       f"API 오류 {len(trace['errors'])}건 (최대 깊이 {opts.get('max_depth')})")
    summary.append("HL 계정: " + ", ".join(f"hop {h} {n}개" for h, n in sorted(hop_count.items()))
                   + f" (송금 추적 {opts.get('hl_hops')}홉, 상한 {opts.get('max_accounts')}개)")
    hubs = [a for a in accounts.values() if a.hub]
    if hubs:
        summary.append(f"허브 의심 계정 {len(hubs)}개에서 추적 종료: " + ", ".join(f"`{a.address}`" for a in hubs))
    if eth_accounts:
        eth_hops = Counter(a.hop for a in eth_accounts.values())
        stops = Counter(a.stop for a in eth_accounts.values() if a.stop)
        stops.update(l.kind.split(" ")[0] for a in eth_accounts.values() for l in a.leads if " 입금" in l.kind)
        summary.append("Ethereum 주소: " + ", ".join(f"hop {h} {n}개" for h, n in sorted(eth_hops.items()))
                       + f" (추적 {opts.get('eth_hops')}홉, 상한 {opts.get('max_eth')}개)"
                       + (" · 종착 " + ", ".join(f"{k} {n}건" for k, n in stops.items()) if stops else ""))
    if eth_failed:
        summary.append(f"수집 실패 ETH 주소 {len(eth_failed)}개 — 결과는 부분 조회")
    summary.append("다음 체인 리드: " + (", ".join(f"{c} {n}건" for c, n in by_chain.most_common()) or "없음"))
    if failed:
        summary.append(f"수집 실패 HL 계정 {len(failed)}개 — 결과는 부분 조회")
    parts += [f"- {s}" for s in summary] + ["", mermaid(start, accounts, eth_accounts), ""]

    parts += ["## 1. 단계별 연결", "",
              table(["단계", "데이터", "연결고리", "확실도 결정"], [
                  ["BTC 시작점 → BTC 입금 output", "Esplora tx / outspends", "UTXO 소비 관계 (outpoint)",
                   "경로에 다중 입력 tx가 있으면 estimated"],
                  ["BTC 입금 → HL 계정", "HyperUnit /operations", "sourceTxHash(txid:vout) + 금액 + protocol 주소",
                   "done·금액 일치 시 confirmed/estimated"],
                  ["HL 계정 내부", "HL 원장·체결", "원장 nonce ↔ Unit operation", "hl_ledger 판정"],
                  ["HL 계정 → HL 계정", "HL 원장 send/spotTransfer", "송금 hash", "추적 자금 외 유입 비율 (1% 기준)"],
                  ["HL → 외부 체인", "HL 원장 + HyperUnit", "출금 nonce → Unit destinationTxHash", "출금 자체는 확정"],
                  ["Ethereum 주소 → 주소", "Blockscout v2 (tx · internal tx · USDC/USDT/WETH/DAI)", "tx hash",
                   "추적 자금 외 유입·이전 잔액 비율 (1% 기준). 주소 오염·가짜 토큰 제외, DEX 스왑은 전환으로 이음"],
              ]), "",
              "경로 확실도 = 시작점부터 그 지점까지 구간 확실도 중 가장 낮은 것. "
              "`확정` > `계정 단위 확정` (계정 안에서 합쳐져 건별 매핑은 추정) > `추정` > `미확인`.", ""]

    if trace:
        rows = [[m["depth"], _tx("bitcoin", m["btc_source_tx"]) + f":{m['vout']}", m["amount_btc"],
                 _addr("hyperliquid", m["hyperliquid_account"]) if m["hyperliquid_account"] else "-",
                 m["hyperunit_state"], m["confidence"]]
                for m in sorted(trace["matches"], key=lambda m: m["depth"])]
        parts += ["## 2. BTC → HL 입금 매칭", "", table(["깊이", "BTC 입금 tx:vout", "BTC", "HL 계정", "Unit 상태", "확실도"], rows), "",
                  f"> {trace.get('attribution_note', '')}", ""]

    rows = []
    for a in sorted(accounts.values(), key=lambda a: (a.hop, a.key)):
        unit_out = [l for l in a.leads if l.kind == "Unit 출금"]
        rows.append([a.hop, _addr("hyperliquid", a.address), LEVEL_NAME[a.level], LEVEL_NAME[a.path_level],
                     "<br>".join(a.basis), len(a.sends), len(unit_out),
                     f"[report](hl/{a.key}/report.md)"])
    parts += ["## 3. HL 계정", "", table(["hop", "계정", "계정 구간", "경로", "근거", "HL 송금", "Unit 출금", "상세"], rows), ""]

    if edges:
        rows = [[ts(e["time"]), _addr("hyperliquid", e["from"]), _addr("hyperliquid", e["to"]),
                 f"{fmt(e['amount'])} {e['token']}", _usd(e["usdc_value"]),
                 "추적" if e["to"].lower() in accounts else ("HyperEVM" if is_hyperevm_bridge(e["to"]) else "리드"), _tx("hyperliquid", e["hash"])] for e in edges]
        parts += ["## 4. HL 계정 간 송금", "", table(["시각 (UTC)", "보낸 계정", "받은 계정", "수량", "USDC 가치", "처리", "HL tx"], rows), ""]

    if eth_accounts:
        rows = [[a.hop, _addr("ethereum", a.address), a.label.text or "-", LEVEL_NAME[a.level], LEVEL_NAME[a.path_level],
                 "<br>".join(a.basis)] for a in sorted(eth_accounts.values(), key=lambda a: (a.hop, a.key))]
        parts += ["## 5. Ethereum 추적", "", table(["hop", "주소", "태그", "주소 구간", "경로", "근거"], rows), ""]
        if eth_edges:
            rows = [[ts(e["time"]), _addr("ethereum", e["from"]),
                     _addr("ethereum", e["to"]) + (f" {e['label']}" if e["label"] else ""),
                     f"{fmt(e['amount'])} {e['asset']}", e.get("result", "-"),
                     md_link(short(e["tx"], 8), ETH_TX.format(e["tx"]))] for e in eth_edges]
            parts += ["### ETH 송금", "", table(["시각 (UTC)", "보낸 주소", "받은 주소", "수량", "처리", "tx"], rows), ""]

    if leads:
        rows = [[ts(l.time), l.kind, l.chain, _addr(l.chain, l.address), f"{fmt(l.amount)} {l.token}", _usd(l.usdc_value),
                 _tx(l.chain, l.tx),
                 _addr("ethereum" if l.source.lower() in eth_accounts else "hyperliquid", l.source),
                 LEVEL_NAME[l.level]] for l in leads]
        parts += ["## 6. 다음 체인 리드", "", "파이프라인이 더 따라가지 않은 목적지. `leads.csv`를 다음 체인 추적 입력으로 쓴다.", "",
                  table(["시각 (UTC)", "종류", "체인", "목적지 주소", "수량", "USDC 가치", "tx", "출처 계정", "경로 확실도"], rows), ""]

    notes = ["주소 공통성·그래프 연결은 소유권·불법성의 증명이 아니다. \"이 자금이 어디로 갔는가\"만 다룬다.",
             "BTC 추적은 최대 깊이·fan-in/out 30·tx 500개 제한이 있어 계정의 Unit 입금 전부에 닿지 않을 수 있다 (3절 근거의 비율).",
             "HL은 오래된 체결을 돌려주지 않는다. 대량 거래 계정은 `기록 누락 의심` 표식을 확인할 것."]
    if trace and trace["errors"]:
        notes.append(f"BTC 추적 API 오류 {len(trace['errors'])}건 — 일부 가지가 중단됨 (btc/trace.json의 errors)")
    if eth_accounts:
        notes.append("Ethereum은 ETH와 USDC/USDT/WETH/DAI만 본다 (그 외 토큰은 가짜 토큰 스팸이 많아 제외). "
                     "거래소·브리지·믹서 판정은 Blockscout 공개 태그 기준이며, 태그 없는 입금 주소는 일반 주소로 따라간다.")
    for addr, err in failed:
        notes.append(f"HL 수집 실패 `{addr}` — {err}")
    for addr, err in eth_failed:
        notes.append(f"ETH 수집 실패 `{addr}` — {err}")
    parts += ["## 7. 주의", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(parts)


def lead_dict(l: Lead) -> dict:
    return {"time_utc": ts(l.time), "kind": l.kind, "chain": l.chain, "address": l.address, "tx": l.tx,
            "token": l.token, "amount": str(l.amount), "usdc_value": "" if l.usdc_value is None else str(l.usdc_value),
            "source_account": l.source, "path_level": LEVEL_NAME[l.level]}


def write(out: Path, start: str, trace: dict | None, accounts: dict[str, Account], edges: list[dict],
          failed: list[tuple[str, str]], opts: dict, eth_accounts: dict | None = None,
          eth_edges: list[dict] | None = None, eth_failed: list[tuple[str, str]] | None = None) -> Path:
    eth_accounts, eth_edges, eth_failed = eth_accounts or {}, eth_edges or [], eth_failed or []
    out.mkdir(parents=True, exist_ok=True)
    if trace:
        (out / "btc").mkdir(exist_ok=True)
        (out / "btc" / "trace.json").write_text(json.dumps(trace, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "pipeline.md").write_text(render(start, trace, accounts, edges, failed, opts, eth_accounts, eth_edges, eth_failed),
                                       encoding="utf-8")
    leads = [lead_dict(l) for l in final_leads(accounts, eth_accounts)]
    with open(out / "leads.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(lead_dict(Lead("", "", "", "", Decimal(0), None, 0, "", "")).keys()))
        w.writeheader()
        w.writerows(leads)
    data = {
        "start": start, "options": opts,
        "accounts": [{"address": a.address, "hop": a.hop, "parents": sorted(a.parents),
                      "hl_verdict": a.res["verdict"].level, "account_level": LEVEL_NAME[a.level],
                      "path_level": LEVEL_NAME[a.path_level], "basis": a.basis,
                      "btc_matches": [m["match_id"] for m in a.btc["matches"]] if a.btc else []}
                     for a in accounts.values()],
        "hl_edges": [{**e, "amount": str(e["amount"]),
                      "usdc_value": None if e["usdc_value"] is None else str(e["usdc_value"])} for e in edges],
        "eth_accounts": [{"address": a.address, "hop": a.hop, "parents": sorted(a.parents), "label": a.label.text,
                          "account_level": LEVEL_NAME[a.level], "path_level": LEVEL_NAME[a.path_level],
                          "stop": a.stop, "basis": a.basis} for a in eth_accounts.values()],
        "eth_edges": [{**e, "amount": str(e["amount"])} for e in eth_edges],
        "leads": leads,
        "failed": [{"address": a, "error": e} for a, e in failed + eth_failed],
    }
    (out / "pipeline.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return out / "pipeline.md"
