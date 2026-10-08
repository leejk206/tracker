# build/watch/serve 공통: node(nvm)·PDF용 Chromium 찾기
cd "$(dirname "${BASH_SOURCE[0]}")" && mkdir -p out
command -v node >/dev/null || { [ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"; }
command -v node >/dev/null || { echo "node 없음 — nvm install --lts" >&2; exit 1; }
[ -d node_modules ] || npm install --no-fund --no-audit
# PDF용 브라우저: 시스템 Chrome 없으면 Playwright Chromium 사용
export CHROME_PATH="${CHROME_PATH:-$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | tail -1)}"
SRC="${1:-발표.md}"; NAME="$(basename "$SRC" .md)"
MARP=(npx marp -c marp.config.js --no-stdin)
