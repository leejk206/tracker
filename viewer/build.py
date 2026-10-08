"""pipeline 결과(out/<사례>/) → 뷰어용 그래프 데이터 + 정적 페이지.

    python3 viewer/build.py                        # viewer/index.html (정적 호스팅에 그대로 올림)
    python3 viewer/build.py --fragment page.html   # 문서 골격 없는 조각도 같이 저장 (임베드용)

사례 목록은 CASES. out/<사례>/pipeline.json 이 있어야 한다 (python3 -m pipeline ... --out out/<사례>).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

CASES = [
    {
        "id": "case1",
        "title": "사례 1",
        "name": "BTC → HL → ETH → Tron",
        "summary": "BTC 120개가 입금마다 새 HL 계정으로 들어간 뒤 Ethereum에서 쪼개지고, 스왑과 브리지를 거쳐 Tron의 USDT로 도착",
        "end": "Tron 주소 15개 · 약 1,670만 USDT",
    },
    {
        "id": "case2",
        "title": "사례 2",
        "name": "XMR1 분산 후 재집결",
        "summary": "HL 계정 1개가 BTC로 XMR1을 사서 23개 계정에 나눠 보냈고, 23개 모두 같은 서비스 지갑으로 다시 보냄",
        "end": "Wagyu.xyz 서비스 지갑 (모네로 전환 추정)",
    },
    {
        "id": "case3",
        "title": "사례 3",
        "name": "스테이킹 후 거래소",
        "summary": "HL에서 출금된 ETH 244,860개가 약 3개월 스테이킹된 뒤 원금과 보상이 회수되어 Binance로 이동",
        "end": "Binance 핫월렛 (마지막 구간 추정)",
    },
]

CHAIN_ORDER = ["bitcoin", "hyperliquid", "ethereum", "arbitrum", "base", "optimism", "polygon", "tron", "hyperevm", "solana"]
CHAIN_NAME = {
    "bitcoin": "Bitcoin", "hyperliquid": "Hyperliquid", "ethereum": "Ethereum", "arbitrum": "Arbitrum",
    "base": "Base", "optimism": "Optimism", "polygon": "Polygon", "tron": "Tron", "hyperevm": "HyperEVM", "solana": "Solana",
}
EDGE_KIND = {"추적": "전송", "스왑": "스왑", "브리지": "브리지 입금", "리드": "전송", "거래소": "거래소 입금"}


def cidx(chain):
    return CHAIN_ORDER.index(chain) if chain in CHAIN_ORDER else len(CHAIN_ORDER)


def nid(chain, addr):
    return f"{chain}:{addr.lower()}"


def chain_sections(d):
    """새 형식(chains: {체인: {accounts, edges}})과 예전 형식(eth_*/tron_*)을 같은 모양으로."""
    if "chains" in d:
        return {c: (v.get("accounts", []), v.get("edges", [])) for c, v in d["chains"].items()}
    out = {}
    for c, pre in (("ethereum", "eth"), ("tron", "tron")):
        acc, ed = d.get(f"{pre}_accounts", []), d.get(f"{pre}_edges", [])
        if acc or ed:
            out[c] = (acc, ed)
    return out


class Graph:
    def __init__(self):
        self.nodes, self.edges = {}, {}

    def node(self, n):
        self.nodes.setdefault(n["id"], n)
        return n["id"]

    def edge(self, src, dst, kind, amount=None, tx=None, **kw):
        if src not in self.nodes or dst not in self.nodes or src == dst:
            return
        key = (src, dst, kind, kw.get("asset", ""))
        e = self.edges.get(key)
        if e is None:
            e = self.edges[key] = {"source": src, "target": dst, "kind": kind, "count": 0, "amount": 0.0, "txs": [], **kw}
        e["count"] += 1
        try:
            e["amount"] += float(amount or 0)
        except ValueError:
            pass
        if tx and tx not in e["txs"] and len(e["txs"]) < 20:
            e["txs"].append(tx)

    def find(self, addr, *chains, exclude=None):
        for c in chains:
            if c and nid(c, addr) in self.nodes and nid(c, addr) != exclude:
                return nid(c, addr)
        return None


def build_case(meta):
    base = ROOT / "out" / meta["id"]
    d = json.loads((base / "pipeline.json").read_text())
    g = Graph()
    start = d["start"]
    sections = chain_sections(d)

    # 노드: HL 계정
    for a in d.get("accounts", []):
        g.node({
            "id": nid("hyperliquid", a["address"]), "chain": "hyperliquid", "address": a["address"],
            "hop": a.get("hop", 0), "role": "start" if a["address"].lower() == start.lower() else "account",
            "level": a.get("account_level") or a.get("hl_verdict") or "", "path": a.get("path_level", ""),
            "label": "", "basis": a.get("basis", []),
        })

    # 노드: 시작 BTC (tx 또는 주소)
    if not start.startswith("0x"):
        g.node({
            "id": "bitcoin:start", "chain": "bitcoin", "address": start, "hop": 0, "role": "start",
            "level": "", "path": "", "label": "BTC tx" if len(start) == 64 else "BTC 주소", "basis": ["추적 시작점"],
        })

    # 노드: 체인별 주소
    for chain, (accs, _) in sections.items():
        for a in accs:
            g.node({
                "id": nid(chain, a["address"]), "chain": chain, "address": a["address"], "hop": a.get("hop", 0),
                "role": "account", "level": a.get("account_level", ""), "path": a.get("path_level", ""),
                "label": a.get("label", ""), "basis": a.get("basis", []),
            })

    # 노드: 리드 (추적을 멈춘 곳). 이미 추적한 주소면 그 노드에 표시만
    for ld in d.get("leads", []):
        if not ld.get("address"):
            continue
        chain = ld.get("chain") or "unknown"
        lid = nid(chain, ld["address"])
        if lid in g.nodes:
            kinds = g.nodes[lid].setdefault("leadKinds", [])
            if ld["kind"] not in kinds and ld["kind"] != g.nodes[lid].get("label"):
                kinds.append(ld["kind"])
            continue
        src = g.find(ld["source_account"], chain, "hyperliquid", "ethereum", "tron")
        hop = g.nodes[src]["hop"] + 1 if src and g.nodes[src]["chain"] == chain else 0
        g.node({
            "id": lid, "chain": chain, "address": ld["address"], "hop": hop, "role": "lead", "level": "리드",
            "path": ld.get("path_level", ""), "label": ld["kind"], "basis": [ld["kind"]],
        })

    # 간선: BTC → HL (HyperUnit 입금)
    trace = base / "btc" / "trace.json"
    if "bitcoin:start" in g.nodes and trace.exists():
        for m in json.loads(trace.read_text()).get("matches", []):
            g.edge("bitcoin:start", nid("hyperliquid", m["hyperliquid_account"]), "HyperUnit 입금",
                   amount=m.get("amount_btc"), tx=m.get("btc_source_tx"), asset="BTC")

    # 간선: HL 송금
    for e in d.get("hl_edges", []):
        dst = nid("hyperliquid", e["to"])
        if dst not in g.nodes:
            dst = g.find(e["to"], "hyperevm") or dst
        g.edge(nid("hyperliquid", e["from"]), dst, "HL 송금", amount=e.get("amount"), tx=e.get("hash"), asset=e.get("token", ""))

    # 간선: HL → 외부 체인 (HyperUnit 출금). 계정별 events.csv
    for a in d.get("accounts", []):
        ev = base / "hl" / a["address"] / "events.csv"
        if not ev.exists():
            continue
        with ev.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["label"] != "Unit 출금" or r.get("unit_state") == "failure" or not r.get("unit_chain_address"):
                    continue
                g.edge(nid("hyperliquid", a["address"]), nid(r["unit_chain"], r["unit_chain_address"]), "HyperUnit 출금",
                       amount=r["amount"], tx=r.get("unit_chain_tx") or r.get("hl_hash"), asset=r["token"].removeprefix("U"))
    # events.csv로 못 이은 hop 0 주소는 parents(HL 계정)로
    for chain, (accs, _) in sections.items():
        for a in accs:
            dst = nid(chain, a["address"])
            if a.get("hop") == 0 and not any(k[1] == dst for k in g.edges):
                for p in a.get("parents", []):
                    if nid("hyperliquid", p) in g.nodes:
                        g.edge(nid("hyperliquid", p), dst, "HyperUnit 출금")

    # 간선: 체인 내부 이동. 스왑·브리지 컨트랙트로 들어간 건 노드에 메모로
    for chain, (_, eds) in sections.items():
        for e in eds:
            src = nid(chain, e["from"])
            if e.get("result") in ("스왑", "브리지") and src in g.nodes and nid(chain, e["to"]) not in g.nodes:
                via = (e.get("label") or e["to"]).split(" · ")[0]
                note = f'{e["result"]}: {via}'
                if note not in g.nodes[src].setdefault("notes", []):
                    g.nodes[src]["notes"].append(note)
            g.edge(nid(chain, e["from"]), nid(chain, e["to"]), EDGE_KIND.get(e.get("result"), e.get("result") or "전송"),
                   amount=e.get("amount"), tx=e.get("tx"), asset=e.get("asset", ""), via=e.get("label", ""))

    # 간선: 브리지 (체인 간)
    for b in d.get("bridges", []):
        src = g.find(b["src_address"], "ethereum", "arbitrum", "base", "optimism", "polygon")
        g.edge(src or "", nid(b["dst_chain"], b["receiver"]), b["protocol"], amount=b.get("amount"), tx=b.get("src_tx"),
               asset=b.get("token", ""), dst_tx=b.get("dst_tx", ""), status=b.get("status", ""))

    # 간선: 아직 안 이어진 리드
    linked = {k[1] for k in g.edges}
    for ld in d.get("leads", []):
        lid = nid(ld.get("chain") or "unknown", ld.get("address") or "")
        if lid in g.nodes and lid not in linked:
            src = g.find(ld["source_account"], ld.get("chain"), "hyperliquid", "ethereum", "tron", exclude=lid)
            if src:
                g.edge(src, lid, "리드", amount=ld.get("amount"), tx=ld.get("tx"), asset=ld.get("token", ""))

    nodes = list(g.nodes.values())
    for n in nodes:
        n["col"] = [cidx(n["chain"]), n["hop"]]
        n["chainName"] = CHAIN_NAME.get(n["chain"], n["chain"])
    edges = list(g.edges.values())
    for e in edges:
        e["amount"] = round(e["amount"], 6)
    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "leads": sum(n["role"] == "lead" or bool(n.get("leadKinds")) for n in nodes),
        "chains": sorted({n["chain"] for n in nodes}, key=cidx),
        "levels": {lv: sum(n["level"] == lv for n in nodes) for lv in ("확정", "계정 단위 확정", "추정")},
    }
    return {**meta, "start": start, "stats": stats, "nodes": nodes, "edges": edges}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "index.html"))
    ap.add_argument("--fragment", help="문서 골격(doctype/head/body) 없는 조각으로도 저장")
    args = ap.parse_args()
    data = []
    for meta in CASES:
        if not (ROOT / "out" / meta["id"] / "pipeline.json").exists():
            print(f"건너뜀: out/{meta['id']}/pipeline.json 없음", file=sys.stderr)
            continue
        c = build_case(meta)
        data.append(c)
        print(f"{c['id']}: 노드 {c['stats']['nodes']} · 간선 {c['stats']['edges']} · 리드 {c['stats']['leads']}")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    frag = (HERE / "template.html").read_text().replace("/*__DATA__*/null", blob)
    if args.fragment:
        Path(args.fragment).write_text(frag.replace("<!--BODY-->", ""))
        print("→", args.fragment)
    page = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            + frag.replace("<!--BODY-->", "</head>\n<body>") + "\n</body>\n</html>\n")
    Path(args.out).write_text(page)
    print("→", args.out)


if __name__ == "__main__":
    main()
