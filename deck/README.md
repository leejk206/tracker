# deck — 발표자료 (Markdown → PDF)

`발표.md` 하나가 소스. `defi/deck` 파이프라인 이식 (Marp).

```bash
bash deck/watch.sh          # 저장할 때마다 deck/out/발표.pdf 자동 재렌더 (Ctrl+C 종료)
bash deck/build.sh          # 한 번 렌더: deck/out/발표.pdf + 발표.html
bash deck/serve.sh          # 브라우저 미리보기 http://localhost:8080/발표.md (저장 즉시 리로드)
bash deck/build.sh 다른.md  # 다른 md 파일도 같은 테마로
```

- WSL에서 실행. node는 nvm에서 자동으로 잡고, 첫 실행 때 `npm install`이 자동으로 돈다
- PDF용 브라우저: 시스템 Chrome → 없으면 `~/.cache/ms-playwright` Chromium (`CHROME_PATH`로 직접 지정 가능)
- 테마 `theme.css` · 문법 확장 `engine.js` — 편집 규칙은 `발표.md` 맨 위 주석 참고
- `deck/out/`, `deck/node_modules/`는 git에 올라가지 않음
