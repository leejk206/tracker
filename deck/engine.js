// Marp 엔진 확장 — 발표.md 문법 확장 (defi/deck에서 이식)
//   ::: grid g3 … :::   → <div class="grid g3">   (중첩은 콜론 수로: 바깥 ::::, 안 :::)
//   ::: box / stat acc / cols / div / center / flow / tier / bars
//   ::: flow 안의 `A → **B** → C` 한 줄 → 노드 흐름도 (**굵게** = 강조 노드)
//   표 셀이 `+N%`이면 초록, `−N%`(N ≥ 15)이면 빨강. 첫 셀이 **굵게**인 행은 하이라이트
const container = require('markdown-it-container')

const NAMES = ['grid', 'box', 'stat', 'cols', 'div', 'center', 'flow', 'tier', 'bars']

module.exports = ({ marp }) => {
  for (const name of NAMES) {
    marp.use(container, name, {
      render(tokens, idx) {
        const t = tokens[idx]
        if (t.nesting === 1) {
          const extra = t.info.trim().slice(name.length).trim()
          const cls = name === 'div' ? extra : `${name} ${extra}`.trim()
          return cls ? `<div class="${cls}">\n` : '<div>\n'
        }
        return '</div>\n'
      },
    })
  }

  marp.markdown.core.ruler.push('deck_post', (state) => {
    const toks = state.tokens
    let inFlow = 0
    for (let i = 0; i < toks.length; i++) {
      const t = toks[i]
      if (t.type === 'container_flow_open') inFlow++
      else if (t.type === 'container_flow_close') inFlow--
      else if (inFlow && t.type === 'inline' && t.content.includes('→')) {
        const parts = t.content.split('→').map((s) => s.trim())
        t.children = []
        t.content = parts
          .map((p) => {
            const m = p.match(/^\*\*(.+)\*\*$/)
            return m ? `<span class="node k">${m[1]}</span>` : `<span class="node">${p}</span>`
          })
          .join('<span class="arr">→</span>')
        t.type = 'html_block'
        t.content = `<p>${t.content}</p>`
        // 감싸는 paragraph 토큰 제거
        if (toks[i - 1] && toks[i - 1].type === 'paragraph_open') toks[i - 1].hidden = true
        if (toks[i + 1] && toks[i + 1].type === 'paragraph_close') toks[i + 1].hidden = true
      }
      // 표 셀 색
      if (t.type === 'td_open' && toks[i + 1] && toks[i + 1].type === 'inline') {
        const v = toks[i + 1].content.trim()
        const m = v.match(/^([+\-−])(\d+)%$/)
        if (m) {
          const n = (m[1] === '+' ? 1 : -1) * Number(m[2])
          if (n > 0) t.attrJoin('class', 'up')
          else if (n <= -15) t.attrJoin('class', 'dn')
        }
      }
      if (t.type === 'tr_open') {
        const first = toks[i + 2]
        let cells = 0
        for (let j = i + 1; j < toks.length && toks[j].type !== 'tr_close'; j++) if (toks[j].type === 'td_open') cells++
        if (cells >= 5 && toks[i + 1] && toks[i + 1].type === 'td_open' && first && first.type === 'inline' && /^\*\*.+\*\*/.test(first.content.trim())) {
          t.attrJoin('class', 'hi')
        }
      }
      // 표: 열 6개 이상이면 compact, 헤더가 전부 비면 thead 숨김
      if (t.type === 'table_open') {
        let ths = 0, empty = true
        for (let j = i + 1; j < toks.length && toks[j].type !== 'thead_close'; j++) {
          if (toks[j].type === 'th_open') { ths++; if (toks[j + 1].content.trim()) empty = false }
        }
        if (ths >= 6) t.attrJoin('class', 'compact')
        if (empty) t.attrJoin('class', 'nohead')
      }
    }
  })
  return marp
}
