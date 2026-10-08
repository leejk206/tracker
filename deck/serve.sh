#!/usr/bin/env bash
# 브라우저 미리보기 (저장 즉시 자동 리로드)   http://localhost:8080/발표.md
. "$(dirname "$0")/_env.sh"
"${MARP[@]}" -s .
