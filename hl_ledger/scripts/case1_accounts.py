"""사례 1 테스트 입력 재현: 경유 BTC 주소 10개(수기 정리 표) → 각 주소의 출력 주소마다 HyperUnit 조회 → HL 계정.

    python3 scripts/case1_accounts.py     # tests/fixtures/case1_accounts.txt 갱신 (79개, 360.54 BTC)
"""
import json
import time
import urllib.request
from pathlib import Path

VIA = """bc1q3utfynk447c3730vszq4dn72decmzvr660jckq bc1q2zk34zmhucfa2red4644eyfndz5utxwxwlygps
bc1qug3kl873f4vvp2feuawwp749tj8d3z3lkgs8zz bc1qhgy2kwuen90h9ypaney2jj2ww85d20p52jtpv8
bc1qgzdhz7rmd9a443wuxex3cc32n24hdhdrkvcxc3 bc1qq3ngetgk0e4mlvwh9sdwu499crgsr3zfxrtvf9
bc1qes26r33t7m5d2tan4rc0tlg68q0sr7llt675rf bc1qvwj46zsugkdc73rcktppf9l9m7vltmearfudeh
bc1qdma2gxljejfwz328vrtst5qkjyklcfnnekh5rx bc1qe6alnhfkr4c9dretsdxharcslca72u9509clmf""".split()
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "case1_accounts.txt"


def get(url):
    for i in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hl-ledger/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError(url)


def txs(addr):
    """Esplora 주소 tx 전량 (25건씩 /chain/{마지막 txid}로 페이지 이동)."""
    out, last = [], None
    while True:
        page = get(f"https://mempool.space/api/address/{addr}/txs" + (f"/chain/{last}" if last else ""))
        seen = {x["txid"] for x in out}
        new = [t for t in page if t["txid"] not in seen]
        if not new:
            break
        out += new
        last = page[-1]["txid"]
        if len(page) < 25:
            break
    return out


def main():
    accts = {}
    for a in VIA:
        outs = set()
        for t in txs(a):
            if any(v.get("prevout", {}).get("scriptpubkey_address") == a for v in t["vin"]):
                outs |= {o.get("scriptpubkey_address") for o in t["vout"]} - {a, None}
        n0 = len(accts)
        for o in outs:
            for op in get(f"https://api.hyperunit.xyz/operations/{o}").get("operations", []):
                if op["sourceChain"] == "bitcoin" and op["sourceAddress"] == a:
                    accts.setdefault(op["destinationAddress"], []).append(int(op["sourceAmount"]) / 1e8)
        print(a, "출력 주소", len(outs), "새 HL 계정", len(accts) - n0, flush=True)
    total = sum(x for v in accts.values() for x in v)
    print("HL 계정", len(accts), "입금", sum(len(v) for v in accts.values()), "BTC", round(total, 4))
    OUT.write_text("\n".join(accts) + "\n")


if __name__ == "__main__":
    main()
