#!/usr/bin/env bash
# md 저장할 때마다 PDF 자동 재렌더 (Ctrl+C로 종료)   사용: bash deck/watch.sh [파일.md]
. "$(dirname "$0")/_env.sh" "$@"
echo "감시 중: deck/$SRC → deck/out/$NAME.pdf"
"${MARP[@]}" -w "$SRC" -o "out/$NAME.pdf"
