import React from 'react'

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
const fmtTok = n => n == null ? '—' : (n >= 100000000 ? (n/100000000).toFixed(2)+'亿' : n >= 10000 ? (n/10000).toFixed(1)+'万' : n.toLocaleString())
const fmtDur = ms => { if (ms == null) return '—'; const s = ms/1000;
  if (s < 60) return s.toFixed(1)+'秒'; if (s < 3600) return Math.floor(s/60)+'分 '+Math.floor(s%60)+'秒';
  return Math.floor(s/3600)+'小时 '+Math.floor((s%3600)/60)+'分' }
const fmtT = ms => ms ? new Date(ms).toLocaleString() : '—'
const COMP_LABEL = { completed:'完成', completed_with_anomaly:'完成·异常', interrupted:'中断', error:'错误', aborted:'中止', 'max-tokens':'达上限' }

export default function Report({ metrics, result }) {
  const m = metrics || {}
  const tokTotal = (m.input_tokens||0) + (m.cache_read_tokens||0) + (m.output_tokens||0)
  const cacheTotal = (m.input_tokens||0) + (m.cache_read_tokens||0)
  const cacheHit = cacheTotal ? (100*(m.cache_read_tokens||0)/cacheTotal).toFixed(1)+'%' : '-'
  const end = m.turn_end_reason || '—'
  const judge = m.judge || {}
  // 耗时构成：整体 duration；模型活跃 llm_ms；工具 tool_ms；等待输入 human_wait_ms；其余=空闲
  const overall = m.duration_ms_official != null ? m.duration_ms_official : m.duration_ms
  const llm = m.llm_ms ?? 0
  const tool = m.tool_ms ?? 0
  const wait = m.human_wait_ms ?? 0
  const idle = overall != null ? Math.max(0, overall - llm - tool - wait) : 0
  const seg = (v, color) => overall ? { pct: (v / overall * 100).toFixed(1), color } : null

  const stats = [
    { label: 'Token 分项', value: fmtTok(tokTotal),
      sub: `in ${fmtTok(m.input_tokens)} · cr ${fmtTok(m.cache_read_tokens)} · cw ${fmtTok(m.cache_write_tokens)} · out ${fmtTok(m.output_tokens)} · Cache ${cacheHit}` },
    { label: '任务', value: judge.success === undefined ? '—' : (judge.success ? 'PASS' : 'FAIL'),
      cls: judge.success === undefined ? '' : (judge.success ? 'ok' : 'bad'),
      sub: judge.success === undefined ? '判级待跑' : `${judge.level} · ${judge.by || judge.source || ''}` },
    { label: '轮次', value: m.user_turns ?? '—', sub: '真实用户指令数（跨 agent 口径统一）' },
    { label: '结束原因', value: COMP_LABEL[end] || end,
      cls: end==='completed'?'ok':(['error','aborted','interrupted'].includes(end)?'bad':'warn') },
    { label: '工具成功率', value: m.tool_success_rate == null ? '—' : (m.tool_success_rate*100).toFixed(0)+'%',
      sub: `${m.tool_success??0}✓ ${m.tool_fail??0}✗`,
      cls: m.tool_success_rate==null?'':m.tool_success_rate>=.8?'ok':m.tool_success_rate>=.5?'warn':'bad' },
    { label: '成本 ¥', value: m.cost_cny != null ? m.cost_cny.toFixed(4)
        : (m.cost_est_cny != null ? '~' + m.cost_est_cny.toFixed(4) : '—'),
      sub: m.cost_cny != null ? (m.cost_est_cny != null ? `估算 ~¥${m.cost_est_cny.toFixed(4)}` : `结算 ¥${m.cost_cny.toFixed(4)}`)
        : (m.cost_est_cny != null ? '挂牌价估算' : '无价格数据') },
    { label: '模型', value: m.model || '—',
      sub: m.model_turns ? Object.entries(m.model_turns).map(([k, v]) => `${k}×${v}`).join(' ') : '' },
    { label: '总耗时', value: fmtDur(overall), sub: '会话首末事件时间差' },
    { label: '模型活跃', value: fmtDur(llm), sub: 'step/start→step/end 累计（模型真正在跑）' },
    { label: '等待输入', value: wait > 0 ? fmtDur(wait) : '—',
      cls: wait > 0 ? 'warn' : '', sub: 'AskUserQuestion/question 挂起（等人）' },
    { label: '工具调用', value: m.tool_calls_total ?? '—', sub: '总工具调用次数' },
  ]

  // 耗时构成可视化条
  const segs = [
    seg(llm, '#58a6ff'), seg(tool, '#2da44e'), seg(wait, '#d29922'), seg(idle, '#30363d'),
  ].filter(Boolean)

  const dist = m.tool_calls_by_name || {}
  const total = m.tool_calls_total || Object.values(dist).reduce((a, b) => a + b, 0)
  const toolRows = Object.entries(dist).sort((a, b) => b[1] - a[1])
  const mEntries = Object.entries(m.model_turns || {})
  const mTotal = mEntries.reduce((a, [, c]) => a + c, 0)
  const tasks = m.tasks || []

  return (
    <>
      <div className="panel"><h2>总览仪表盘</h2>
        {/* 耗时构成：整体 = 模型活跃 + 工具 + 等待输入 + 空闲 */}
        {overall != null && (
          <div className="dur-breakdown">
            <div className="dur-bar">
              {segs.map((s, i) => (
                <div key={i} className="dur-seg" style={{ width: s.pct + '%', background: s.color }}
                     title={`${['模型活跃', '工具', '等待输入', '空闲'][i]} ${s.pct}%`} />
              ))}
            </div>
            <div className="dur-legend">
              <span><i style={{ background: '#58a6ff' }} />模型活跃 {fmtDur(llm)}</span>
              <span><i style={{ background: '#2da44e' }} />工具 {fmtDur(tool)}</span>
              <span><i style={{ background: '#d29922' }} />等待输入 {fmtDur(wait)}</span>
              <span><i style={{ background: '#30363d' }} />空闲 {fmtDur(idle)}</span>
              <span className="dur-total">整体 {fmtDur(overall)}</span>
            </div>
          </div>
        )}
        <div className="grid">
          {stats.map(s => (
            <div className="stat" key={s.label}>
              <div className="label">{s.label}</div>
              <div className={`value ${s.cls || ''}`}>{s.value}</div>
              {s.sub ? <div className="label sub">{s.sub}</div> : null}
            </div>
          ))}
        </div>
      </div>

      <div className="panel"><h2>工具与模型</h2>
        <div className="cols2">
          <div>
            <div className="muted lbl">工具调用分布（共 {total} 次）</div>
            <div className="barlist">
              {toolRows.length ? toolRows.map(([name, cnt]) => {
                const pct = total ? (100 * cnt / total).toFixed(1) : 0
                const fail = (m.tool_fail_by_name || {})[name] || 0
                const cls = name.startsWith('mcp__') ? 'mcp'
                  : (name === 'skill' || name === 'Skill') ? 'skill'
                    : (name === 'ask_user_question' || name === 'AskUserQuestion') ? 'ask' : 'tool'
                return (
                  <div className="bar-row" key={name}>
                    <span className="bar-name" title={name}>{name}
                      {fail > 0 && <span className="bar-fail" title={`失败 ${fail} 次`}>✗{fail}</span>}
                    </span>
                    <div className="bar-bg"><div className={`bar-fill ${cls}`} style={{ width: pct + '%' }} /></div>
                    <span className="bar-val">{cnt} · {pct}%</span>
                  </div>
                )
              }) : <div className="muted empty">无</div>}
            </div>
          </div>
          <div>
            <div className="muted lbl">模型使用</div>
            <div className="barlist">
              {mEntries.length ? mEntries.map(([mdl, cnt]) => {
                const pct = mTotal ? (100 * cnt / mTotal).toFixed(1) : 0
                return (
                  <div className="bar-row" key={mdl}>
                    <span className="bar-name" title={mdl}>{mdl}</span>
                    <div className="bar-bg"><div className="bar-fill model" style={{ width: pct + '%' }} /></div>
                    <span className="bar-val">{cnt} · {pct}%</span>
                  </div>
                )
              }) : <div className="muted empty">无</div>}
            </div>
          </div>
        </div>
      </div>
    </>
  )
}
