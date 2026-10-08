# viewer — 추적 결과 웹 뷰어

`out/<사례>/pipeline.json`을 단계별 그래프로 보여주는 정적 페이지 (`index.html` 한 파일, 데이터 포함).

```bash
python3 viewer/build.py     # out/case1~3 → viewer/index.html
```

브라우저로 `viewer/index.html`을 열면 된다. 서버 필요 없음.

- 열 = 체인과 단계(hop), 왼쪽 → 오른쪽이 자금 이동 순서
- 노드 색 = 연결 확실도 (확정 · 계정 단위 확정 · 추정 · 리드)
- 보라색 선 = HyperUnit · deBridge · LayerZero 기록으로 체인 사이를 이은 구간
- 노드를 누르면 시작점까지의 경로, 판정 근거, 들어오고 나간 거래(탐색기 링크)가 패널에 나온다
- `#case2`처럼 주소 끝에 사례 id를 붙이면 그 사례로 바로 열림

사례를 추가하려면 `python3 -m pipeline ... --out out/<id>`로 결과를 만들고 `build.py`의 `CASES`에 한 줄 추가.

## GitHub Pages로 공개

`viewer/index.html`을 커밋한 뒤 저장소 Settings → Pages → Source를 `main` 브랜치로 지정하면
`https://<계정>.github.io/<저장소>/viewer/` 에서 열린다.

`build.py`는 표준 라이브러리만 쓴다. 페이지는 그래프 렌더링에 Cytoscape.js(cdnjs)를 불러온다.
