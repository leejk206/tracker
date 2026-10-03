#!/usr/bin/env bash
# 저장소 전체 오프라인 테스트: pipeline · hl_ledger · hyperliquid-tracer. 네트워크를 쓰지 않는다.
set -u
cd "$(dirname "$0")"
status=0

run() {
  echo "== $1"
  shift
  if ! "$@"; then
    status=1
  fi
}

run "pipeline" python3 -m pytest pipeline/tests -q
run "hl_ledger" bash -c "cd hl_ledger && python3 -m pytest -q"
run "hyperliquid-tracer (unit)" bash -c "cd hyperliquid-tracer && python3 -m unittest discover -s tests"
run "hyperliquid-tracer (저장된 실제 결과)" bash -c "cd hyperliquid-tracer && python3 tests/verify_saved_cases.py"

if [ "$status" -eq 0 ]; then echo "== 전부 통과"; else echo "== 실패 있음"; fi
exit "$status"
