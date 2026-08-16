import React, { useEffect, useState } from 'react'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtTok = n => n == null ? '—' : (n >= 100000000 ? (n/100000000).toFixed(2)+'亿' : n >= 10000 ? (n/10000).toFixed(1)+'万' : n.toLocaleString())
const fmtT = ms => ms ? new Date(ms).toLocaleString('zh-CN', { hour12: false }) : '—'
const LEVELS = ['L1', 'L2', 'L3', 'L4']
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', L4: '#cf222e' }

// 评测矩阵：能力画像（agent×L1-L4 SR + 平均指标）+ 会话明细
export default function EvalMatrix() {
  const [data, setData] = useState(null)

  useEffect(() => { refresh() }, [])
  function refresh() {
    fetch('/api/eval-matrix').then(r => r.json()).then(setData).catch(() => {})
  }

  if (!data) return <div className="muted" style={{ padding: 20 }}>加载中…（完成会话评测后自动收录）</div>
  const { portrait, records } = data

  // 画像：按 agent 分组，行=agent、列=L1-L4
  const byAgent = {}
  for (const p of portrait) (byAgent[p.agent] = byAgent[p.agent] || []).push(p)
  const agents = Object.keys(byAgent)

  return (
    <>
      <div className="panel">
        <h2>能力画像 · 共 {records.length} 条评测记录 <button className="ghost" onClick={refresh} style={{ float: 'right' }}>↻</button></h2>
        {agents.length === 0 ? <div className="muted empty" style={{ padding: 10 }}>暂无评测记录——点选会话评测后自动判级收录</div> : (
          <table>
            <thead><tr><th>agent</th><th className="num">评测数</th>
              {LEVELS.map(l => <th key={l} className="num">{l} SR</th>)}
              <th className="num">平均工具</th><th className="num">平均 token</th><th className="num">平均成本¥</th></tr></thead>
            <tbody>
              {agents.map(ag => {
                const cells = {}; let total = 0, tools = 0, toks = 0, cost = 0
                for (const p of byAgent[ag]) {
                  cells[p.level] = p
                  total += p.count; tools += p.avg_tools * p.count; toks += p.avg_tokens_in * p.count; cost += p.avg_cost_cny * p.count
                }
                return (
                  <tr key={ag}>
                    <td className="ktype">{ag}</td>
                    <td className="num">{total}</td>
                    {LEVELS.map(l => {
                      const c = cells[l]
                      return <td key={l} className="num" style={{ color: c ? (c.sr >= 0.8 ? 'var(--green)' : c.sr >= 0.5 ? 'var(--yellow)' : 'var(--red)') : 'var(--ink3)' }}>
                        {c ? (c.sr * 100).toFixed(0) + '%' : '—'}
                        <span style={{ fontSize: 9, color: 'var(--ink3)' }}>{c ? ` (${c.success}/${c.count})` : ''}</span>
                      </td>
                    })}
                    <td className="num">{total ? (tools / total).toFixed(1) : '—'}</td>
                    <td className="num">{total ? fmtTok(Math.round(toks / total)) : '—'}</td>
                    <td className="num">{total ? (cost / total).toFixed(4) : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="panel">
        <h2>会话明细 · {records.length}</h2>
        <table>
          <thead><tr><th>时间</th><th>agent</th><th>级别</th><th>成功</th><th>会话</th><th>任务</th>
            <th className="num">工具</th><th className="num">token</th><th className="num">成本¥</th><th>结束</th></tr></thead>
          <tbody>
            {records.map((r, i) => (
              <tr key={i}>
                <td className="num muted">{r._at ? fmtT(r._at).slice(5, 16) : '—'}</td>
                <td>{r.agent}</td>
                <td><span className="badge" style={{ background: (LEVEL_COLOR[r.level] || '#6e7681') + '33', color: LEVEL_COLOR[r.level] || '#6e7681' }}>
                  {r.level}{r.level_source === 'auto' ? '*' : ''}</span></td>
                <td style={{ color: r.success ? 'var(--green)' : 'var(--red)' }}>{r.success ? '✓' : '✗'}</td>
                <td className="mono" style={{ fontSize: 11 }} title={r.session_id}>{String(r.session_id || '').slice(0, 16)}</td>
                <td className="tq" title={r.query}>{esc(r.query || '')}</td>
                <td className="num">{r.tool_calls_total ?? '—'}</td>
                <td className="num">{fmtTok(r.input_tokens)}</td>
                <td className="num">{r.cost_cny != null ? r.cost_cny.toFixed(4) : '—'}</td>
                <td className="muted" style={{ fontSize: 11 }}>{r.turn_end_reason || '—'}</td>
              </tr>
            ))}
            {!records.length && <tr><td className="empty" colSpan="10" style={{ padding: 12 }}>暂无记录</td></tr>}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>* 自动推断（task 匹配优先） · 成功判定：task 校验器 / 结束原因兜底</div>
      </div>
    </>
  )
}
