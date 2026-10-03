"""사용법:
    python -m hl_ledger 0x3A37880ff9EbD7B45376Ef15bDA69Cefc0016575
    python -m hl_ledger 0xAAA… 0xBBB… --out out/case1
    python -m hl_ledger --file accounts.txt --out out/case1
    python -m hl_ledger --raw out/0x3a37…/raw.json        # 저장된 원자료로 재분석 (네트워크 없음)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import api
from .analyze import analyze
from .report import render_markdown, render_summary, write_all

ADDR = re.compile(r"0x[0-9a-fA-F]{40}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hl_ledger", description="HL 계정 입출금·체결 내역을 표로 뽑는다")
    p.add_argument("addresses", nargs="*", help="HL 계정 주소 (0x…)")
    p.add_argument("--file", type=Path, help="주소 목록 파일 (줄마다 0x… 하나, 다른 텍스트 섞여도 됨)")
    p.add_argument("--raw", type=Path, help="이전에 저장한 raw.json으로 재분석")
    p.add_argument("--out", type=Path, default=Path("out"), help="출력 폴더 (기본 out/)")
    p.add_argument("--print", action="store_true", help="단일 계정이면 report.md를 터미널에도 출력")
    a = p.parse_args(argv)

    for path in (a.raw, a.file):
        if path and not path.is_file():
            p.error(f"파일 없음: {path}")

    failed: list[tuple[str, str]] = []
    if a.raw:
        raws = [json.loads(a.raw.read_text(encoding="utf-8"))]
    else:
        addrs = list(a.addresses)
        if a.file:
            addrs += ADDR.findall(a.file.read_text(encoding="utf-8"))
        bad = [x for x in addrs if not ADDR.fullmatch(x)]
        if bad or not addrs:
            p.error(f"HL 주소(0x + 40자리)가 필요함: {bad or '입력 없음'}")
        addrs = list(dict.fromkeys(addrs))  # 순서 유지 중복 제거
        raws = []
        for i, addr in enumerate(addrs, 1):
            print(f"[{i}/{len(addrs)}] {addr} 수집 중…", file=sys.stderr)
            try:
                raws.append(api.fetch_all(addr))
            except api.ApiError as e:
                # 한 계정 실패로 일괄 조회 전체를 버리지 않는다
                print(f"  실패: {e}", file=sys.stderr)
                failed.append((addr, str(e)))
        if not raws:
            print("수집 성공한 계정 없음", file=sys.stderr)
            return 1

    results = []
    for raw in raws:
        res = analyze(raw)
        dest = write_all(res, a.out / raw["user"].lower() if len(raws) > 1 else a.out)
        v = res["verdict"]
        print(f"{raw['user']}  {v.level}  → {dest / 'report.md'}", file=sys.stderr)
        results.append(res)

    if len(results) > 1 or failed:
        summary = render_summary(results)
        if failed:
            summary += "\n## 수집 실패 (다시 실행하면 됨)\n\n" + "\n".join(f"- `{x}` — {err}" for x, err in failed) + "\n"
        (a.out / "summary.md").write_text(summary, encoding="utf-8")
        print(f"요약 → {a.out / 'summary.md'}", file=sys.stderr)
    if len(results) == 1 and a.print:
        print(render_markdown(results[0]))
    if failed:
        print(f"수집 실패 {len(failed)}개: " + ", ".join(x for x, _ in failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
