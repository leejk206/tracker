"""분석 결과 → 마크다운(노션 붙여넣기용) / CSV / JSON."""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from .analyze import Event, fmt, ts

EXPLORER = {
    "bitcoin": "https://mempool.space/tx/{}",
    "ethereum": "https://etherscan.io/tx/{}",
    "solana": "https://solscan.io/tx/{}",
}
HL_TX = "https://app.hyperliquid.xyz/explorer/tx/{}"
LOOKUP = ("Hyperliquid POST /info (userNonFundingLedgerUpdates, userFillsByTime, spotClearinghouseState) "
          "+ HyperUnit GET /operations/{주소}")


def short(s: str, n: int = 6) -> str:
    return s if len(s) <= 2 * n + 1 else f"{s[:n + 2]}…{s[-n:]}"


def chain_tx(op: dict, which: str) -> str:
    """Unit op의 원천/목적 체인 tx. BTC는 'txid:vout' 형식이라 txid만 쓴다."""
    chain = op[f"{which}Chain"]
    tx = op[f"{which}TxHash"] or ""
    if chain == "bitcoin":
        tx = tx.split(":")[0]
    return tx


def md_link(text: str, url: str | None) -> str:
    return f"[{text}]({url})" if url else text


def _chain_link(op: dict, which: str) -> str:
    tx = chain_tx(op, which)
    tpl = EXPLORER.get(op[f"{which}Chain"])
    return md_link(short(tx, 8), tpl.format(tx) if tpl and tx else None) if tx else "-"


def table(headers: list[str], rows: list[list]) -> str:
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", "<br>")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def _list_or_more(items: list[str], limit: int = 3) -> str:
    if len(items) <= limit:
        return "<br>".join(items)
    return "<br>".join(items[:limit]) + f"<br>외 {len(items) - limit}건 (아래 상세 표)"


def stage_table(res: dict) -> str:
    """사례 표 양식: 단계 | tx 해시 또는 HL 기록 | 연결고리 식별자 | 연결 확실도 및 근거 | 사용한 조회 경로."""
    user = res["raw"]["user"]
    ev: list[Event] = res["events"]
    v = res["verdict"]
    unit_in = [e for e in ev if e.unit and e.direction == "in"]
    unit_out = [e for e in ev if e.unit and e.direction == "out"]
    hl_out = [e for e in ev if e.direction == "out" and not e.unit]
    rows = []

    if unit_in:
        srcs = [f"{e.unit['sourceChain']} {short(chain_tx(e.unit, 'source'), 8)} · {ts(e.time)} · "
                f"{fmt(e.amount)} {e.token}" for e in unit_in]
        chains = sorted({e.unit["sourceChain"] for e in unit_in})
        rows.append([
            f"{'/'.join(chains)} → HL 입금 (HyperUnit)",
            _list_or_more(srcs),
            "① 원천 체인 tx (Unit sourceTxHash)<br>② Unit 입금 주소 (protocolAddress)<br>"
            "③ HL 계정 (destinationAddress)<br>④ HL 원장 nonce = Unit destinationTxHash의 nonce",
            f"확정. Unit API가 원천 tx와 HL 계정을 직접 연결하고, HL 원장 입금 {len(unit_in)}건의 nonce가 "
            f"Unit 기록과 전건 일치",
            "HyperUnit GET /operations/{HL 계정} + Hyperliquid userNonFundingLedgerUpdates",
        ])

    trades = [f"{g.pair} {g.side} {g.count}건 · {fmt(g.size)} · VWAP {fmt(g.vwap, 4)}" for g in res["fills"]]
    rows.append([
        "HL 내부 (매매)",
        f"HL 계정 {user}<br>" + (_list_or_more(trades, 4) if trades else "체결 없음"),
        "① HL 계정 ② userFills 체결 (코인·수량·가격·시각·hash) ③ 입금 후 첫 체결까지 시간 ④ 잔액",
        f"{v.level}. {v.summary}<br>" + "<br>".join(f"· {r}" for r in v.reasons),
        "Hyperliquid POST /info → userFillsByTime, spotClearinghouseState",
    ])

    if unit_out or hl_out:
        items = [f"{e.unit['destinationChain']} {short(chain_tx(e.unit, 'destination'), 8)} · {ts(e.time)} · "
                 f"{fmt(e.amount)} {e.token} → {short(e.unit['destinationAddress'])}" for e in unit_out]
        items += [f"{e.label} · {ts(e.time)} · {fmt(e.amount)} {e.token} → {short(e.counterparty)}" for e in hl_out]
        out_level = "출금 자체는 확정" if unit_out else "HL 내부 송금은 확정"
        link_level = {"확정": "입금과의 연결도 확정", "계정 단위 확정": "입금과는 계정 단위로 확정, 건별은 추정"}.get(
            v.level, "입금과의 연결은 추정")
        rows.append([
            "HL → 외부 출금 / HL 내 송금",
            _list_or_more(items),
            "① HL 원장 nonce = Unit sourceTxHash의 nonce ② 목적 체인 tx (Unit destinationTxHash) "
            "③ 목적지 주소 ④ 송금 수신 HL 계정",
            f"{out_level} ({len(unit_out)}건 Unit 출금, {len(hl_out)}건 HL 송금). {link_level}",
            "HyperUnit operations + Hyperliquid userNonFundingLedgerUpdates + 목적 체인 익스플로러",
        ])
    return table(["단계", "해당 tx 해시 또는 HL 기록", "연결고리가 되는 식별자", "연결 확실도 및 근거", "사용한 조회 경로"], rows)


def event_rows(events: list[Event], direction: str) -> list[list]:
    rows = []
    for i, e in enumerate([e for e in events if e.direction == direction], 1):
        if e.unit:
            which = "source" if direction == "in" else "destination"
            other = e.unit["sourceAddress"] if direction == "in" else e.unit["destinationAddress"]
            ext = f"{e.unit[which + 'Chain']} {_chain_link(e.unit, which)}"
        else:
            other, ext = e.counterparty, "-"
        rows.append([
            i, ts(e.time), e.label, e.token, fmt(e.amount),
            fmt(e.usdc_value, 2) if e.usdc_value is not None else "-",
            md_link(short(e.hash, 6), HL_TX.format(e.hash)), ext,
            short(other) if other.startswith(("0x", "bc1")) or len(other) > 30 else other,
        ])
    return rows


def render_markdown(res: dict) -> str:
    raw, ev, v = res["raw"], res["events"], res["verdict"]
    user = raw["user"]
    ins = event_rows(ev, "in")
    outs = event_rows(ev, "out")
    internal = [e for e in ev if e.direction == "internal"]
    first_in = min((e.time for e in ev if e.unit and e.direction == "in"), default=None)
    first_fill = min((f["time"] for f in raw["fills"] if first_in is None or f["time"] >= first_in), default=None)

    parts = [
        f"# HL 계정 입출금 — `{user}`",
        f"조회 시각 {ts(raw['fetchedAt'])} UTC · 시간은 전부 UTC · "
        f"[Hypurrscan](https://hypurrscan.io/address/{user})",
        "",
        f"**판정: {v.level}** — {v.summary}",
        "",
        *[f"- {r}" for r in v.reasons],
        *[f"- ⚠ {n}" for n in v.notes],
    ]
    if first_in and first_fill:
        caveat = " — 체결 기록 잘림 의심, 실제 첫 체결은 더 이를 수 있음" if "기록 누락 의심" in v.tags else ""
        parts.append(f"- 첫 Unit 입금 {ts(first_in)} → 첫 체결 {ts(first_fill)} "
                     f"({(first_fill - first_in) / 60000:,.1f}분 후){caveat}")
    parts += ["", "## 단계별 연결 (사례 표 양식)", "", stage_table(res)]

    in_head = ["#", "시각", "유형", "토큰", "수량", "USDC 가치", "HL hash", "원천 체인 tx", "보낸 쪽"]
    out_head = ["#", "시각", "유형", "토큰", "수량", "USDC 가치", "HL hash", "목적 체인 tx", "받는 쪽"]
    parts += ["", f"## 입금 ({len(ins)}건)", "", table(in_head, ins) if ins else "없음"]

    parts += ["", f"## 체결 요약 ({len(raw['fills'])}건)", ""]
    if res["fills"]:
        parts.append(table(
            ["마켓", "방향", "건수", "수량", "체결대금(USDC)", "VWAP", "수수료", "첫 체결", "마지막 체결"],
            [[g.pair, g.side, g.count, fmt(g.size), fmt(g.notional, 2), fmt(g.vwap, 4), fmt(g.fee, 4),
              ts(g.first), ts(g.last)] for g in res["fills"]]))
    else:
        parts.append("없음")

    parts += ["", f"## 출금 · 송금 ({len(outs)}건)", "", table(out_head, outs) if outs else "없음"]

    if internal:
        parts += ["", f"계정 내부 이동 {len(internal)}건 (현물↔선물 등)은 유입·유출에서 제외했다."]
    bal = [b for b in raw.get("balances", []) if Decimal(b["total"]) != 0]
    parts += ["", "## 현재 현물 잔액", "",
              table(["토큰", "수량"], [[b["coin"], fmt(b["total"])] for b in bal]) if bal else "없음"]
    if res["unmatched_unit"]:
        parts += ["", f"## HL 원장과 짝이 없는 Unit operation ({len(res['unmatched_unit'])}건)", "",
                  table(["생성", "방향", "자산", "상태", "원천 tx", "목적 tx"],
                        [[op["opCreatedAt"][:19].replace("T", " "), f"{op['sourceChain']}→{op['destinationChain']}",
                          op["asset"], op["state"], _chain_link(op, "source"), _chain_link(op, "destination")]
                         for op in res["unmatched_unit"]])]
    parts += ["", f"조회 경로: {LOOKUP}", ""]
    return "\n".join(parts)


def write_csv(res: dict, out: Path) -> None:
    with open(out / "events.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "direction", "label", "ledger_type", "token", "amount", "usdc_value", "hl_hash",
                    "nonce", "counterparty", "unit_chain", "unit_chain_tx", "unit_chain_address", "unit_state"])
        for e in res["events"]:
            which = "source" if e.direction == "in" else "destination"
            u = e.unit or {}
            w.writerow([ts(e.time), e.direction, e.label, e.kind, e.token, e.amount,
                        "" if e.usdc_value is None else e.usdc_value, e.hash, e.nonce, e.counterparty,
                        u.get(which + "Chain", ""), chain_tx(u, which) if u else "",
                        u.get(which + "Address", ""), u.get("state", "")])
    names = res["raw"].get("spotPairs", {})
    with open(out / "fills.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "market", "coin", "side", "px", "sz", "fee", "fee_token", "hash", "oid", "tid"])
        for f in res["raw"]["fills"]:
            w.writerow([ts(f["time"]), names.get(f["coin"], f["coin"]), f["coin"], "B" if f["side"] == "B" else "A",
                        f["px"], f["sz"], f.get("fee"), f.get("feeToken"), f["hash"], f["oid"], f["tid"]])


def write_all(res: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(render_markdown(res), encoding="utf-8")
    write_csv(res, out)
    (out / "raw.json").write_text(json.dumps(res["raw"], ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def summary_row(res: dict) -> list:
    ev, v = res["events"], res["verdict"]
    unit_in = [e for e in ev if e.unit and e.direction == "in"]
    by_tok: dict[str, Decimal] = {}
    for e in unit_in:
        by_tok[e.token] = by_tok.get(e.token, Decimal(0)) + e.amount
    return [
        res["raw"]["user"],
        len(unit_in), ", ".join(f"{fmt(a)} {t}" for t, a in by_tok.items()) or "-",
        len([e for e in ev if e.direction == "in" and not e.unit]),
        len(res["raw"]["fills"]),
        len([e for e in ev if e.unit and e.direction == "out"]),
        len([e for e in ev if e.direction == "out" and not e.unit]),
        v.level,
        ", ".join(v.tags) or "-",
    ]


def render_summary(results: list[dict]) -> str:
    head = ["HL 계정", "Unit 입금", "입금량", "기타 유입", "체결", "Unit 출금", "HL 송금·기타 유출", "판정", "표식"]
    counts: dict[str, int] = {}
    tags: dict[str, int] = {}
    for r in results:
        counts[r["verdict"].level] = counts.get(r["verdict"].level, 0) + 1
        for t in r["verdict"].tags:
            tags[t] = tags.get(t, 0) + 1
    return "\n".join([
        f"# HL 계정 일괄 조회 ({len(results)}개)", "",
        "판정: " + " · ".join(f"{k} {n}개" for k, n in counts.items()),
        "표식: " + (" · ".join(f"{k} {n}개" for k, n in tags.items()) or "없음"), "",
        table(head, [summary_row(r) for r in results]), "",
        "계정별 상세는 같은 폴더의 `<계정>/report.md`.", "",
    ])
