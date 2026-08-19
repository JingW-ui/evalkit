import React, { useEffect, useState } from 'react'
import { getStats, getExecutions, reviewExecution, cleanupExecutions } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
// 耗时自适应单位：<60s → "42s"；≥60s → "3m20s" / "3m"
const fmtDur = ms => {
  if (ms == null) return '—'
  const s = ms / 1000
  if (s < 60) return s.toFixed(0) + 's'
  const m = Math.floor(s / 60)
  const rem = Math.floor(s % 60)
  return rem > 0 ? `${m}m${rem}s` : `${m}m`
}
// 成本：<0.01 保留 4 位小数，否则 3 位
const fmtCost = v => v == null ? '—' : (v < 0.01 ? v.toFixed(4) : v.toFixed(3))
const fmtPct = x => x == null ? '—' : (x * 100).toFixed(0) + '%'
const fmtTok = n => n == null ? '—' : (n >= 10000 ? (n / 10000).toFixed(1) + '万' : n.toLocaleString())
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', L4: '#cf222e' }

// 统计总览：task 级总表（agent/模型/SR/均值±σ）→ 展开 n 次执行 → 人工复核
export default function StatsPanel({ onOpenSession }) {
  const [rows, setRows] = useState(null)
  const [open, setOpen] = useState(null)
  const [execs, setExecs] = useState([])
  const [reviewing, setReviewing] = useState(null)
  const [fAgent, setFAgent] = useState('')
  const [fSkill, setFSkill] = useState('')
  const [fLevel, setFLevel] = useState('')
  const [fModel, setFModel] = useState('')

  useEffect(() => { refresh() }, [])
  function refresh() { getStats().then(setRows).catch(() => {}) }
  async function doCleanup() {
    if (!window.confirm('清理无效执行记录（task_id 为空或不在当前题库）？')) return
    await cleanupExecutions()
    refresh()
  }

  const rowKey = r => (r.task_id || '') + '|' + (r.model || '')

  async function toggle(r) {
    const k = rowKey(r)
    if (open === k) { setOpen(null); return }
    setOpen(k)
    setExecs(await getExecutions(r.task_id, r.model))
  }

  async function saveReview(sid, patch) {
    const j = await reviewExecution(sid, patch)
    if (j.ok) {
      setReviewing(null)
      refresh()
      if (open) {
        const [t, m] = open.split('|')
        setExecs(await getExecutions(t, m))
      }
    }
  }

  if (!rows) return <div className="muted" style={{ padding: 20 }}>加载中…</div>

  const agents = [...new Set(rows.map(r => r.agent).filter(Boolean))]
  const skills = [...new Set(rows.map(r => r.skill_expected).filter(Boolean))]
  const levels = [...new Set(rows.map(r => r.level).filter(Boolean))]
  const models = [...new Set(rows.map(r => r.model).filter(Boolean))]
  const filtered = rows.filter(r =>
    (!fAgent || r.agent === fAgent) && (!fSkill || r.skill_expected === fSkill) &&
    (!fLevel || r.level === fLevel) && (!fModel || r.model === fModel))

  return (
    <div className="panel">
      <h2>统计总览 · {filtered.length}/{rows.length} 个任务
        <button className="ghost" onClick={doCleanup} style={{ float: 'right' }} title="清理无效执行记录（task_id 为空或不在当前题库）">清理无效数据</button>
        <button className="ghost" onClick={refresh} style={{ float: 'right' }}>↻</button></h2>
      <div className="launcher-row" style={{ marginBottom: 8 }}>
        <label>筛选</label>
        <select value={fAgent} onChange={e => setFAgent(e.target.value)}><option value="">agent 全部</option>{agents.map(a => <option key={a} value={a}>{a}</option>)}</select>
        <select value={fSkill} onChange={e => setFSkill(e.target.value)}><option value="">skill 全部</option>{skills.map(s => <option key={s} value={s}>{s}</option>)}</select>
        <select value={fLevel} onChange={e => setFLevel(e.target.value)}><option value="">level 全部</option>{levels.map(l => <option key={l} value={l}>{l}</option>)}</select>
        <select value={fModel} onChange={e => setFModel(e.target.value)}><option value="">模型 全部</option>{models.map(m => <option key={m} value={m}>{m}</option>)}</select>
      </div>
      <table style={{ tableLayout: 'fixed' }}>
        <colgroup>
          <col style={{ width: '16%' }} /><col style={{ width: '8%' }} />
          <col style={{ width: '12%' }} /><col style={{ width: '8%' }} />
          <col style={{ width: '5%' }} /><col style={{ width: '5%' }} />
          <col style={{ width: '10%' }} /><col style={{ width: '18%' }} />
          <col style={{ width: '18%' }} />
        </colgroup>
        <thead><tr>
          <th>任务</th><th>agent</th><th>模型</th><th>skill</th><th>L</th><th className="num">n</th>
          <th className="num">成功率</th><th className="num">耗时(均值±σ)</th><th className="num">成本(均值±σ)</th>
        </tr></thead>
        <tbody>
          {filtered.map(r => (
            <React.Fragment key={rowKey(r)}>
              <tr onClick={() => toggle(r)} style={{ cursor: 'pointer' }}>
                <td className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.task_id}>{esc(r.task_id || '—')}</td>
                <td>{esc(r.agent || '—')}</td>
                <td className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.model}>{esc(r.model || '—')}</td>
                <td>{esc(r.skill_expected || '—')}</td>
                <td><span className="badge" style={{ background: (LEVEL_COLOR[r.level] || '#6e7681') + '33', color: LEVEL_COLOR[r.level] || '#6e7681' }}>{r.level || '?'}</span></td>
                <td className="num">{r.n}</td>
                <td className="num" style={{ color: r.sr >= 0.8 ? 'var(--green)' : r.sr >= 0.5 ? 'var(--yellow)' : 'var(--red)' }}>
                  {fmtPct(r.sr)}
                  <span style={{ fontSize: 9, color: 'var(--ink3)' }}> {r.success}/{r.n}{r.ci_lower != null ? ` · ≥${(r.ci_lower * 100).toFixed(0)}%` : ''}</span>
                  {r.veto && (
                    <span title={r.veto_hit ? 'L4 一票否决：出现幻觉成功，本次验收不通过' : 'L4 诚实题（一票否决）'}
                      style={{ marginLeft: 4, padding: '0 4px', borderRadius: 4, fontSize: 10,
                        background: r.veto_hit ? '#cf222e' : '#6e7681', color: '#fff' }}>
                      veto{r.veto_hit ? '!' : ''}
                    </span>
                  )}
                </td>
                <td className="num">{r.duration_ms != null
                  ? fmtDur(r.duration_ms) + (r.duration_sd != null ? ' ± ' + fmtDur(r.duration_sd) : '')
                  : '—'}</td>
                <td className="num">{r.cost_cny != null
                  ? '¥' + fmtCost(r.cost_cny) + (r.cost_sd != null ? ' ± ' + fmtCost(r.cost_sd) : '')
                  : '—'}</td>
              </tr>
              {open === rowKey(r) && (
                <tr><td colSpan="9" style={{ padding: 0, background: 'var(--bg)' }}>
                  <ExecList execs={execs} reviewing={reviewing} setReviewing={setReviewing} saveReview={saveReview} onOpenSession={onOpenSession} />
                </td></tr>
              )}
            </React.Fragment>
          ))}
          {!filtered.length && <tr><td className="empty" colSpan="9" style={{ padding: 12 }}>暂无统计——先跑批量评测（repeat≥1）</td></tr>}
        </tbody>
      </table>
    </div>
  )
}

function ExecList({ execs, reviewing, setReviewing, saveReview, onOpenSession }) {
  if (!execs.length) return <div className="muted" style={{ padding: 10 }}>无执行记录</div>
  return (
    <table style={{ margin: 0 }}>
      <thead><tr><th>#</th><th>成功</th><th>级别</th><th className="num">耗时</th>
        <th className="num">成本</th><th className="num">工具成功率</th><th className="num">工具次数</th>
        <th className="num">人工介入</th><th className="num">Token(in)</th><th>结束</th><th>操作</th></tr></thead>
      <tbody>
        {execs.map(e => (
          <ReviewRow key={e.session_id} e={e} reviewing={reviewing} setReviewing={setReviewing} saveReview={saveReview} onOpenSession={onOpenSession} />
        ))}
      </tbody>
    </table>
  )
}

function ReviewRow({ e, reviewing, setReviewing, saveReview, onOpenSession }) {
  const [level, setLevel] = useState(e.level)
  const [success, setSuccess] = useState(e.success)
  const [note, setNote] = useState(e.review_note || '')
  const editing = reviewing === e.session_id
  const sf = (e.tool_success || 0) + (e.tool_fail || 0)
  const tsr = sf > 0 ? (e.tool_success || 0) / sf : null
  return (
    <tr>
      <td className="num">r{e.run_idx}</td>
      <td style={{ color: e.success ? 'var(--green)' : 'var(--red)' }}>{e.success ? '✓' : '✗'}</td>
      <td><span className="badge" style={{ background: (LEVEL_COLOR[e.level] || '#6e7681') + '33', color: LEVEL_COLOR[e.level] || '#6e7681' }}>{e.level}</span></td>
      <td className="num">{fmtDur(e.duration_ms)}</td>
      <td className="num">{e.cost_cny != null ? '¥' + fmtCost(e.cost_cny) : '—'}</td>
      <td className="num">{tsr != null ? fmtPct(tsr) : '—'}</td>
      <td className="num">{e.tool_calls_total ?? '—'}</td>
      <td className="num">{e.human_interventions ?? '—'}</td>
      <td className="num">{fmtTok(e.input_tokens)}</td>
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
          <>
            <button className="ghost" onClick={() => onOpenSession && onOpenSession(e.session_id)}>轨迹</button>
            <button className="ghost" onClick={() => setReviewing(e.session_id)}>
              复核{e.review_status === 'corrected' ? '·已修正' : e.review_status === 'invalid' ? '·无效' : ''}
            </button>
            <span title="答辩归因（fail/pass/invalid）" style={{ opacity: 0.85 }}>
              <button className="ghost" title="pass：机器误判，纠正为成功" onClick={() => saveReview(e.session_id, { defense: 'pass' })}>✓</button>
              <button className="ghost" title="fail：agent 能力问题，计入失败" onClick={() => saveReview(e.session_id, { defense: 'fail' })}>✗</button>
              <button className="ghost" title="invalid：题目硬伤，排除统计" onClick={() => saveReview(e.session_id, { defense: 'invalid' })}>∅</button>
            </span>
          </>
        )}
      </td>
    </tr>
  )
}
