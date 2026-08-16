import React, { useState } from 'react'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtMs = ms => ms == null ? '—' : ms >= 3600000 ? (ms/3600000).toFixed(1)+'h'
  : ms >= 60000 ? (ms/60000).toFixed(1)+'m' : ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms'
const fmtTime = ms => ms ? new Date(ms).toLocaleTimeString() : '—'
const fmtTok = n => n == null ? '—' : n.toLocaleString()
export const KIND_LABEL = { input: 'Input', model: 'Model', tool: 'Tool', mcp: 'MCP', skill: 'Skill' }

// 详情面板（对齐 DSH inspector：dl 概览 + 底条 tab + 内容区，右侧圆角悬浮卡片）
export default function Inspector({ record, onClose }) {
  // 每条新记录重置默认 tab：带技能列表的输入记录默认展示技能
  const [lastKey, setLastKey] = useState(null)
  const [dtab, setDtab] = useState(null)
  const key = record ? (record.eidx != null ? record.eidx : record.id) : null
  if (key !== lastKey) {
    setLastKey(key)
    setDtab(null)   // 换记录 → 回落到默认 tab（skills 优先）
  }
  if (!record) return null
  const d = record.detail || {}
  const usage = d.usage
  const payload = d.arguments != null ? d.arguments : d.text
  const tabs = []
  if (payload) tabs.push(['payload', '输入'])
  if (d.result != null) tabs.push(['result', '输出'])
  if (d.thinking) tabs.push(['thinking', '推理'])
  if (d.skills) tabs.push(['skills', `技能 ${d.skills.length}`])
  // 默认 tab：带技能列表的输入记录优先展示技能（点击 user 输入即见技能下拉）；否则首个
  const preferred = d.skills ? 'skills' : null
  const active = tabs.some(t => t[0] === (dtab || preferred)) ? (dtab || preferred) : (tabs[0] ? tabs[0][0] : 'result')
  return (
    <aside className="inspector">
      <div className="inspector-head">
        <span className={`badge ${record.isError ? 'badge-blocked' : 'badge-skill'}`}>
          {KIND_LABEL[record.kind] || record.kind}{record.isError ? ' · fail' : ''}
        </span>
        <span className="iname">{record.name}</span>
        <span className="iloc">{record.seq != null ? '#' + record.seq : ''}</span>
        <button className="ghost iclose" onClick={onClose} title="关闭">✕</button>
      </div>

      <dl className="inspector-overview">
        <div><dt>Duration</dt><dd>{fmtMs(record.dur)}</dd></div>
        <div><dt>Started</dt><dd>{fmtTime(record.start)}</dd></div>
        <div><dt>Status</dt><dd className={record.isError ? 'error' : ''}>{record.isError ? 'fail' : 'ok'}</dd></div>
        {record.callId && <div><dt>Call</dt><dd className="mono">{record.callId}</dd></div>}
        {usage && (
          <div className="token"><dt>Tokens</dt>
            <dd className="mono">in {fmtTok(usage.inputTokens)} · cr {fmtTok(usage.cacheReadTokens)} · cw {fmtTok(usage.cacheWriteTokens)} · out {fmtTok(usage.outputTokens)}</dd>
          </div>
        )}
      </dl>

      <div className="inspector-tabs">
        {tabs.map(([id, label]) => (
          <button key={id} className={`itab ${active === id ? 'active' : ''}`} onClick={() => setDtab(id)}>{label}</button>
        ))}
        {!tabs.length && <span className="iloc" style={{ alignSelf: 'center' }}>无详情</span>}
      </div>

      <div className="inspector-body">
        {active === 'payload' && <pre className="raw">{payload || ''}</pre>}
        {active === 'result' && <pre className="raw">{d.result ?? '(无结果)'}</pre>}
        {active === 'thinking' && <pre className="raw">{d.thinking}</pre>}
        {active === 'skills' && (
          <div className="skill-list">
            {d.skills.map((s, i) => (
              <details key={i} className="skill-item">
                <summary><span className="sk-name">{esc(s.name)}</span></summary>
                <div className="sk-desc">{s.description ? esc(s.description) : <span className="muted">（无描述）</span>}</div>
              </details>
            ))}
            {!d.skills.length && <div className="muted">（空）</div>}
          </div>
        )}
      </div>
    </aside>
  )
}
