import React, { useEffect, useState } from 'react'
import { getStats, getExecutions, reviewExecution } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtDur = ms => ms == null ? '—' : (ms / 1000).toFixed(0) + 's'
const fmtSD = (m, sd) => m == null ? '—' : (sd == null ? `${m}` : `${m} ± ${sd}`)
const fmtPct = x => x == null ? '—' : (x * 100).toFixed(0) + '%'
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', L4: '#cf222e' }

// 统计总览：task 级总表（SR + 均值±σ）→ 展开 n 次执行 → 人工复核
export default function StatsPanel() {
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(null)
  const [execs, setExecs] = useState([])
  const [reviewing, setReviewing] = useState(null)

  useEffect(() => { refresh() }, [])
  function refresh() { getStats().then(setRows).catch(() => {}) }

  async function toggle(taskId) {
    if (open === taskId) { setOpen(null); return }
    setOpen(taskId)
    setExecs(await getExecutions(taskId))
  }

  async function saveReview(sid, patch) {
    const j = await reviewExecution(sid, patch)
    if (j.ok) {
      setReviewing(null)
      refresh()
      if (open) setExecs(await getExecutions(open))
    }
  }

  if (!rows) return <div className="muted" style={{ padding: 20 }}>加载中…</div>

  return (
    <div className="panel">
      <h2>统计总览 · {rows.length} 个任务 <button className="ghost" onClick={refresh} style={{ float: 'right' }}>↻</button></h2>
      <table>
        <thead><tr>
          <th>任务</th><th>skill</th><th>L</th><th className="num">n</th>
          <th className="num">成功率</th><th className="num">耗时(均值±σ)</th>
          <th className="num">成本¥(均值±σ)</th><th className="num">工具成功率</th>
          <th className="num">工具次数</th><th className="num">人工介入</th>
        </tr></thead>
        <tbody>
          {rows.map(r => (
            <React.Fragment key={r.task_id}>
              <tr onClick={() => toggle(r.task_id)} style={{ cursor: 'pointer' }}>
                <td className="tq" title={r.task_id}>{esc(r.task_id || '')}</td>
                <td>{esc(r.skill_expected || '—')}</td>
                <td><span className="badge" style={{ background: (LEVEL_COLOR[r.level] || '#6e7681') + '33', color: LEVEL_COLOR[r.level] || '#6e7681' }}>{r.level || '?'}</span></td>
                <td className="num">{r.n}</td>
                <td className="num" style={{ color: r.sr >= 0.8 ? 'var(--green)' : r.sr >= 0.5 ? 'var(--yellow)' : 'var(--red)' }}>
                  {fmtPct(r.sr)}<span style={{ fontSize: 9, color: 'var(--ink3)' }}> {r.success}/{r.n}</span>
                </td>
                <td className="num">{fmtSD(Math.round(r.duration_ms / 1000), r.duration_sd != null ? Math.round(r.duration_sd / 1000) : null)}</td>
                <td className="num">{r.cost_cny != null ? r.cost_cny.toFixed(3) + (r.cost_sd != null ? ' ± ' + r.cost_sd.toFixed(3) : '') : '—'}</td>
                <td className="num">{fmtPct(r.tool_sr)}{r.tool_sr_sd != null ? <span style={{ fontSize: 9, color: 'var(--ink3)' }}> ±{fmtPct(r.tool_sr_sd)}</span> : null}</td>
                <td className="num">{fmtSD(r.tool_calls, r.tool_calls_sd)}</td>
                <td className="num">{r.human_interventions ?? '—'}</td>
              </tr>
              {open === r.task_id && (
                <tr><td colSpan="10" style={{ padding: 0, background: 'var(--bg)' }}>
                  <ExecList execs={execs} reviewing={reviewing} setReviewing={setReviewing} saveReview={saveReview} />
                </td></tr>
              )}
            </React.Fragment>
          ))}
          {!rows.length && <tr><td className="empty" colSpan="10" style={{ padding: 12 }}>暂无统计——先跑批量评测（repeat≥1）</td></tr>}
        </tbody>
      </table>
      <div className="muted" style={{ fontSize: 10, marginTop: 6 }}>耗时/成本/工具成功率/工具次数为「均值 ± 样本标准差 σ」；成功率不统计方差</div>
    </div>
  )
}

function ExecList({ execs, reviewing, setReviewing, saveReview }) {
  if (!execs.length) return <div className="muted" style={{ padding: 10 }}>无执行记录</div>
  return (
    <table style={{ margin: 0 }}>
      <thead><tr><th>#</th><th>成功</th><th>级别</th><th className="num">耗时</th>
        <th className="num">成本¥</th><th className="num">工具</th><th>结束</th><th>复核</th></tr></thead>
      <tbody>
        {execs.map(e => (
          <ReviewRow key={e.session_id} e={e} reviewing={reviewing} setReviewing={setReviewing} saveReview={saveReview} />
        ))}
      </tbody>
    </table>
  )
}

function ReviewRow({ e, reviewing, setReviewing, saveReview }) {
  const [level, setLevel] = useState(e.level)
  const [success, setSuccess] = useState(e.success)
  const [note, setNote] = useState(e.review_note || '')
  const editing = reviewing === e.session_id
  return (
    <tr>
      <td className="num">r{e.run_idx}</td>
      <td style={{ color: e.success ? 'var(--green)' : 'var(--red)' }}>{e.success ? '✓' : '✗'}</td>
      <td><span className="badge" style={{ background: (LEVEL_COLOR[e.level] || '#6e7681') + '33', color: LEVEL_COLOR[e.level] || '#6e7681' }}>{e.level}</span></td>
      <td className="num">{fmtDur(e.duration_ms)}</td>
      <td className="num">{e.cost_cny != null ? e.cost_cny.toFixed(3) : '—'}</td>
      <td className="num">{e.tool_calls_total ?? '—'}</td>
      <td className="muted" style={{ fontSize: 11 }}>{e.turn_end_reason || '—'}</td>
      <td>
        {editing ? (
          <span className="review-inline">
            <select value={level} onChange={ev => setLevel(ev.target.value)}>
              {['L1', 'L2', 'L3', 'L4', 'L?'].map(l => <option key={l} value={l}>{l}</option>)}
            </select>
            <select value={success ? '1' : '0'} onChange={ev => setSuccess(ev.target.value === '1')}>
              <option value="1">成功</option><option value="0">失败</option>
            </select>
            <input value={note} onChange={ev => setNote(ev.target.value)} placeholder="备注" />
            <button className="ghost" onClick={() => saveReview(e.session_id, { level, success, note })}>保存</button>
            <button className="ghost" onClick={() => saveReview(e.session_id, { reset: true })}>重置</button>
            <button className="ghost" onClick={() => setReviewing(null)}>取消</button>
          </span>
        ) : (
          <button className="ghost" onClick={() => setReviewing(e.session_id)}>
            复核{e.review_status === 'corrected' ? '·已修正' : ''}
          </button>
        )}
      </td>
    </tr>
  )
}
