#!/usr/bin/env bash
# 한 번 렌더: deck/out/<이름>.pdf + .html   사용: bash deck/build.sh [파일.md]
. "$(dirname "$0")/_env.sh" "$@"
"${MARP[@]}" "$SRC" -o "out/$NAME.html" && "${MARP[@]}" "$SRC" -o "out/$NAME.pdf" && echo "→ deck/out/$NAME.pdf"
