// 导出：把当前会话评测视图渲染为自包含 HTML（完整、所见即所得）
import { deriveModel, deriveRows, buildTimeProjection } from './components/TrajectoryView.jsx'

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
const fmtTok = n => n == null ? '—' : (n >= 100000000 ? (n/100000000).toFixed(2)+'亿' : n >= 10000 ? (n/10000).toFixed(1)+'万' : n.toLocaleString())
const fmtDur = ms => { if (ms == null) return '—'; const s = ms/1000;
  if (s < 60) return s.toFixed(1)+'秒'; if (s < 3600) return Math.floor(s/60)+'分 '+Math.floor(s%60)+'秒';
  return Math.floor(s/3600)+'小时 '+Math.floor((s%3600)/60)+'分' }
const fmtDurC = ms => { if (ms == null) return '—'; const s = ms/1000;
  if (s < 60) return s.toFixed(1)+'s'; if (s < 3600) return (s/60).toFixed(1)+'m';
  return (s/3600).toFixed(1)+'h' }
const fmtDT = ms => ms ? new Date(ms).toLocaleString('zh-CN', { hour12: false }) : '—'
const COMP_LABEL = { completed:'完成', completed_with_anomaly:'完成·异常', interrupted:'中断', error:'错误', aborted:'中止', 'max-tokens':'达上限' }
const LANE_NAMES = ['input', 'model', 'tools/mcp/skill']
// 泳道配色（对齐 DSH：user=蓝、message=紫调、tool=黄橙）——三泳道整体色调分离
const KIND_COLOR = { input:'#6e7681', input_user:'#58a6ff', input_sys:'#6e7681', input_wait:'#30363d',
                     model:'#bc8cff', tool:'#2da44e', mcp:'#39c5cf', skill:'#f0883e', ask:'#d29922' }

const CSS = `
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --border2:#21262d; --ink:#e6edf3; --ink2:#8b949e; --ink3:#6e7681;
          --green:#2da44e; --red:#cf222e; --yellow:#d29922; --blue:#58a6ff; --mono:"SF Mono",Consolas,monospace; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--ink); font:12.5px/1.45 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; padding:16px; }
  .wrap { max-width:1280px; margin:0 auto; }
  h1 { font-size:16px; margin-bottom:2px; } h2 { font-size:13px; margin-bottom:8px; }
  .sub { color:var(--ink3); font-size:11px; font-family:var(--mono); margin-bottom:10px; word-break:break-all; }
  .hero { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px 18px; margin-bottom:12px; }
  .hero-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px 16px; }
  .hitem .v { font-size:16px; font-weight:700; color:var(--green); } .hitem .k { color:var(--ink3); font-size:11px; }
  .statstrip { display:flex; flex-wrap:wrap; gap:4px 14px; border-top:1px solid var(--border2); padding-top:8px; margin-top:8px; }
  .ss { } .ss .k { font-size:9px; color:var(--ink3); text-transform:uppercase; } .ss .v { font-size:12px; font-weight:600; font-family:var(--mono); }
  .panel { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 16px; margin-bottom:10px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:8px; }
  .stat { background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:8px 12px; }
  .stat .label { color:var(--ink3); font-size:11px; } .stat .value { font-size:14px; font-weight:600; margin-top:2px; font-family:var(--mono); }
  .stat .value.ok { color:var(--green); } .stat .value.bad { color:var(--red); } .stat .value.warn { color:var(--yellow); }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--border); }
  th { color:var(--ink3); font-weight:600; font-size:11px; } td.num,th.num { text-align:right; font-family:var(--mono); }
  .muted { color:var(--ink3); } .mono { font-family:var(--mono); }
  .barlist { display:flex; flex-direction:column; gap:3px; margin-top:4px; }
  .bar-row { display:flex; align-items:center; gap:8px; font-size:11px; }
  .bar-name { width:150px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ink2); font-family:var(--mono); }
  .bar-bg { flex:1; height:12px; background:var(--border2); border-radius:3px; overflow:hidden; }
  .bar-fill { height:100%; border-radius:3px; } .bf-tool{background:#2da44e}.bf-mcp{background:#39c5cf}.bf-skill{background:#f0883e}.bf-ask{background:#d29922}.bf-model{background:#bc8cff}
  .bar-val { width:80px; text-align:right; color:var(--ink3); font-family:var(--mono); font-size:10px; }
  .subs-block { margin-top:4px; }
  .subs-head { font-size:10px; font-weight:700; color:var(--ink2); margin-bottom:2px; }
  .subtask { display:flex; flex-direction:column; font-size:11px; font-family:var(--mono); padding:1px 0; }
  .subtask-summary { display:flex; gap:6px; align-items:center; }
  .sub-ic { width:14px; text-align:center; } .sub-ic.ok{color:var(--green)} .sub-ic.run{color:var(--yellow)} .sub-ic.bad{color:var(--red)}
  .subtask .subj { color:var(--ink2); } .subtask .subst { color:var(--ink3); font-size:10px; }
  .subtask-detail { padding-left:20px; }
  .toolchain { display:flex; flex-wrap:wrap; align-items:center; gap:2px; font-family:var(--mono); margin-top:4px; }
  .tc-arrow { display:inline-flex; color:var(--ink3); } .tc-arrow svg { display:block; }
  .tc-arrow.ok{color:rgba(46,160,67,.7)} .tc-arrow.bad{color:rgba(248,81,73,.7)}
  .tc-node { display:inline-flex; align-items:center; gap:4px; padding:2px 8px; border-radius:6px; border:1px solid var(--border); background:#1c2128; font-size:11px; }
  .tc-node.ok { border-color:rgba(46,160,67,.5); color:var(--green); } .tc-node.bad { border-color:rgba(248,81,73,.5); color:var(--red); }
  .tc-node .tc-name { font-weight:600; } .tc-dur { color:var(--ink3); font-size:10px; }
  .tl-canvas { border:1px solid var(--border); border-radius:10px; padding:4px 8px; background:var(--bg); }
  .tl-axis { position:relative; height:16px; border-bottom:1px solid var(--border2); }
  .tl-tick { position:absolute; transform:translateX(-50%); color:var(--ink3); font-size:9px; }
  .tl-seg.user { outline:1px solid rgba(63,185,80,.6); }
  .tl-seg.wait { opacity:.75; outline:1px dashed rgba(110,118,129,.5); }
  .tl-seg.compressed { background-image:repeating-linear-gradient(45deg, rgba(110,118,129,.55) 0 3px, rgba(13,17,23,.55) 3px 6px); }
  .tl-seg.ask { outline:1px dashed rgba(210,153,34,.8); }
  .dur-wait { color:var(--yellow); font-weight:600; }
  tr.askrow td { background:rgba(210,153,34,.06); }
  tr.waitrow td { color:var(--ink3); background:rgba(110,118,129,.07); }
  .pt.wait { background:#6e7681; }
  .tl-lane { display:flex; min-height:24px; margin-top:2px; }
  .tl-lane-label { width:92px; font-size:9px; color:var(--ink3); display:flex; align-items:center; text-transform:uppercase; }
  .tl-lane-body { position:relative; flex:1; border-left:1px solid var(--border2); }
  .tl-seg { position:absolute; top:2px; bottom:2px; border-radius:3px; opacity:.85; overflow:hidden; }
  .tl-seg.msg { overflow:visible; min-width:2px; }
  .tl-seg.unresolved { outline:1px dashed rgba(240,136,62,.85); opacity:.9; }
  .tl-seg-label { position:absolute; left:4px; top:50%; transform:translateY(-50%); color:#fff; font-size:9px; white-space:nowrap; text-shadow:0 1px 2px rgba(0,0,0,.8); }
  table.ledger { width:100%; border-collapse:collapse; font-family:var(--mono); font-size:11px; }
  table.ledger th { font-weight:500; color:var(--ink3); font-size:10px; padding:3px 6px; border-bottom:1px solid var(--border); }
  table.ledger td { padding:3px 6px; border-bottom:1px solid var(--border2); }
  .ledger .ktype { color:var(--ink2); } .ledger .content { color:var(--ink2); max-width:0; width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .ledger .dur { color:var(--ink3); text-align:right; }
  .pt { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:4px; }
  .pt.ok{background:var(--green)} .pt.bad{background:var(--red)} .pt.run{background:var(--yellow)}
  .rawlog { white-space:pre-wrap; word-break:break-all; font-family:var(--mono); font-size:11px; color:var(--ink2); line-height:1.4;
            background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 14px; }
  .badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:11px; font-weight:600; margin-right:4px; }
  .badge-skill { background:rgba(45,164,78,.15); color:#3fb950; } .badge-noskill { background:rgba(110,118,129,.18); color:var(--ink3); }
  .badge-blocked { color:var(--red); font-weight:700; }
  .final { white-space:pre-wrap; font-family:var(--mono); font-size:10px; color:var(--ink2); margin-top:4px; }
`

function statstripHtml(m) {
  const judge = m.judge || {}
  const taskVal = judge.success === undefined ? '—' : `${judge.success ? 'PASS' : 'FAIL'} · ${judge.level || '?'}`
  const overall = m.duration_ms_official != null ? m.duration_ms_official : m.duration_ms
  const llm = m.llm_ms ?? 0
  const wait = m.human_wait_ms ?? 0
  const waitPct = overall ? Math.round(wait / overall * 100) : null
  const items = [
    ['开始', fmtDT(m.started_at)], ['耗时', fmtDur(overall)],
    ['模型活跃', fmtDur(llm)], ['等输入', wait > 0 ? fmtDur(wait) + (waitPct != null ? ` (${waitPct}%)` : '') : '—'],
    ['轮次', m.user_turns ?? m.num_turns ?? '—'], ['任务', taskVal],
    ['工具', m.tool_calls_total ?? '—'], ['工具成功率', m.tool_success_rate == null ? '—' : (m.tool_success_rate*100).toFixed(0)+'%'],
    ['Token', fmtTok((m.input_tokens||0)+(m.cache_read_tokens||0)+(m.output_tokens||0))],
    ['in', fmtTok(m.input_tokens)], ['out', fmtTok(m.output_tokens)], ['cacheR', fmtTok(m.cache_read_tokens)],
    ['成本¥', m.cost_cny != null ? m.cost_cny.toFixed(3) : (m.cost_est_cny != null ? '~'+m.cost_est_cny.toFixed(3) : '—')],
    ['模型', m.model || '—'], ['Skill', m.skill_loaded || '—'],
  ]
  return `<div class="statstrip">${items.map(([k, v]) => `<div class="ss"><span class="k">${k}</span><br><span class="v">${esc(v)}</span></div>`).join('')}</div>`
}

function reportHtml(m) {
  const tokTotal = (m.input_tokens||0) + (m.cache_read_tokens||0) + (m.output_tokens||0)
  const cacheTotal = (m.input_tokens||0) + (m.cache_read_tokens||0)
  const cacheHit = cacheTotal ? (100*(m.cache_read_tokens||0)/cacheTotal).toFixed(1)+'%' : '-'
  const end = m.turn_end_reason || '—'
  const judge = m.judge || {}
  const overall = m.duration_ms_official != null ? m.duration_ms_official : m.duration_ms
  const llm = m.llm_ms ?? 0
  const tool = m.tool_ms ?? 0
  const wait = m.human_wait_ms ?? 0
  const idle = overall != null ? Math.max(0, overall - llm - tool - wait) : 0
  const durBar = overall != null
    ? `<div style="display:flex;height:8px;border-radius:4px;overflow:hidden;margin-bottom:4px">` +
      [[llm,'#58a6ff'],[tool,'#2da44e'],[wait,'#d29922'],[idle,'#30363d']].map(([v,c]) =>
        `<div style="width:${(v/overall*100).toFixed(1)}%;background:${c}"></div>`).join('') + `</div>` +
      `<div style="font-size:10px;color:var(--ink2)">` +
      `<span style="margin-right:10px"><i style="background:#58a6ff"></i>模型活跃 ${fmtDur(llm)}</span>` +
      `<span style="margin-right:10px"><i style="background:#2da44e"></i>工具 ${fmtDur(tool)}</span>` +
      `<span style="margin-right:10px"><i style="background:#d29922"></i>等待输入 ${fmtDur(wait)}</span>` +
      `<span><i style="background:#30363d"></i>空闲 ${fmtDur(idle)}</span>` +
      `<span style="float:right;font-weight:600">整体 ${fmtDur(overall)}</span></div>`
    : ''
  const stats = [
    ['Token 分项', fmtTok(tokTotal), `in ${fmtTok(m.input_tokens)} · cr ${fmtTok(m.cache_read_tokens)} · cw ${fmtTok(m.cache_write_tokens)} · out ${fmtTok(m.output_tokens)} · Cache ${cacheHit}`, ''],
    ['任务', judge.success === undefined ? '—' : (judge.success ? 'PASS' : 'FAIL'),
     judge.success === undefined ? '判级待跑' : `${judge.level} · ${judge.by || judge.source || ''}`,
     judge.success === undefined ? '' : (judge.success ? 'ok' : 'bad')],
    ['轮次', m.user_turns ?? '—', '真实用户指令数（跨 agent 口径统一）', ''],
    ['结束原因', COMP_LABEL[end] || end, '', end==='completed' ? 'ok' : (['error','aborted','interrupted'].includes(end) ? 'bad' : 'warn')],
    ['工具成功率', m.tool_success_rate == null ? '—' : (m.tool_success_rate*100).toFixed(0)+'%', `${m.tool_success??0}✓ ${m.tool_fail??0}✗`, m.tool_success_rate==null?'':m.tool_success_rate>=.8?'ok':m.tool_success_rate>=.5?'warn':'bad'],
    ['成本 ¥', m.cost_cny != null ? m.cost_cny.toFixed(4) : (m.cost_est_cny != null ? '~'+m.cost_est_cny.toFixed(4) : '—'), '结算/挂牌估算', ''],
    ['总耗时', fmtDur(overall), '会话首末事件时间差', ''],
    ['模型活跃', fmtDur(llm), 'step/start→step/end 累计', ''],
    ['等待输入', wait > 0 ? fmtDur(wait) : '—', 'AskUserQuestion/question 挂起', wait > 0 ? 'warn' : ''],
  ]
  // 工具条形
  const dist = m.tool_calls_by_name || {}
  const total = m.tool_calls_total || Object.values(dist).reduce((a, b) => a + b, 0)
  const toolRows = Object.entries(dist).sort((a, b) => b[1] - a[1])
  const bars = toolRows.map(([name, cnt]) => {
    const pct = total ? (100*cnt/total).toFixed(1) : 0
    const fail = (m.tool_fail_by_name || {})[name] || 0
    const cls = name.startsWith('mcp__') ? 'bf-mcp' : (name==='skill'||name==='Skill') ? 'bf-skill'
      : (name === 'AskUserQuestion' || name === 'ask_user_question' || name === 'question') ? 'bf-ask' : 'bf-tool'
    return `<div class="bar-row"><span class="bar-name" title="${esc(name)}">${esc(name)}${fail ? ` <span style="color:var(--red)">✗${fail}</span>` : ''}</span><div class="bar-bg"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div><span class="bar-val">${cnt} · ${pct}%</span></div>`
  }).join('')
  const mEntries = Object.entries(m.model_turns || {})
  const mTotal = mEntries.reduce((a, [, c]) => a + c, 0)
  const mBars = mEntries.map(([mdl, cnt]) => {
    const pct = mTotal ? (100*cnt/mTotal).toFixed(1) : 0
    return `<div class="bar-row"><span class="bar-name">${esc(mdl)}</span><div class="bar-bg"><div class="bar-fill bf-model" style="width:${pct}%"></div></div><span class="bar-val">${cnt} · ${pct}%</span></div>`
  }).join('')
  return `<div class="panel"><h2>总览仪表盘</h2>${durBar ? `<div style="margin-bottom:8px">${durBar}</div>` : ''}<div class="grid">${stats.map(s =>
    `<div class="stat"><div class="label">${s[0]}</div><div class="value ${s[3]}">${s[1]}</div>${s[2] ? `<div class="label" style="margin-top:4px">${s[2]}</div>` : ''}</div>`).join('')}</div></div>
    <div class="panel"><h2>工具与模型</h2><div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
      <div><div class="muted" style="font-size:11px;margin-bottom:6px">工具调用分布（共 ${total} 次）</div>${bars || '<div class="muted">无</div>'}</div>
      <div><div class="muted" style="font-size:11px;margin-bottom:6px">模型使用</div>${mBars || '<div class="muted">无</div>'}</div>
    </div></div>`
}

function tasksHtml(m) {
  const tasks = m.tasks || []
  if (!tasks.length) return ''
  const SUB_ICON = { completed:'✓', in_progress:'↻', blocked:'✕', pending:'·' }
  const arrow = state => `<span class="tc-arrow ${state}"><svg width="14" height="8" viewBox="0 0 14 8" fill="none"><path d="M0 4h11" stroke="currentColor" stroke-width="1.2"/><path d="M11 1l3 3-3 3z" fill="currentColor"/></svg></span>`
  const node = tc => `<span class="tc-node ${tc.ok === false ? 'bad' : tc.ok === true ? 'ok' : ''}"><span class="tc-name">${esc(tc.name)}</span>${tc.dur_ms != null ? `<span class="tc-dur"> ${tc.dur_ms}ms</span>` : ''}${tc.ok === false ? ' ✗' : tc.ok === true ? ' ✓' : ''}</span>`
  const chain = tools => tools.map((tc, j) => `${j ? arrow(tc.ok === false ? 'bad' : tc.ok === true ? 'ok' : '') : ''}${node(tc)}`).join('')
  // 子任务 → 工具归属（与 TasksPanel.linkSubTools 同逻辑：窗口 [created, updated]，
  // updated=TaskUpdate 完成时间；退化窗口不参与，避免 Infinity 吞工具）
  const link = tk => {
    const tools = tk.tools || []
    const subs = (tk.subitems || []).map(s => ({ ...s, tools: [], _valid: false }))
    for (let i = 0; i < subs.length; i++) {
      const s = subs[i]
      if (s.created_ms == null) continue
      const hi = s.updated_ms != null && s.updated_ms > s.created_ms ? s.updated_ms : null
      if (hi == null) continue
      s._w = [s.created_ms, hi]
      s._valid = true
    }
    const loose = []
    for (const tc of tools) {
      if (tc.name === 'TaskCreate' || tc.name === 'TaskUpdate') continue
      const t = tc.call_ms
      let placed = null
      for (const s of subs) { if (s._valid && t >= s._w[0] && t <= s._w[1]) { placed = s; break } }
      if (placed) placed.tools.push(tc); else loose.push(tc)
    }
    return { subs, loose }
  }
  const rows = tasks.map((tk, i) => {
    const dur = tk.end_ms && tk.start_ms ? ((tk.end_ms - tk.start_ms)/1000).toFixed(0)+'s' : '—'
    const { subs, loose } = link(tk)
    const subsHtml = subs.map(s => {
      const chainHtml = s.tools.length ? chain(s.tools) : '<span class="muted" style="font-size:10px">无工具调用</span>'
      return `<div class="subtask"><div class="subtask-summary"><span class="sub-ic ${SUB_CLS[s.status] || 'muted'}">${SUB_ICON[s.status] || '·'}</span><span class="subj">${esc(s.subject || s.status)}</span><span class="subst ${SUB_CLS[s.status] || ''}">${s.status}</span></div><div class="subtask-detail"><div class="toolchain">${chainHtml}</div></div></div>`
    }).join('')
    const looseHtml = loose.length ? `<div class="subs-head">任务级工具链</div><div class="toolchain">${chain(loose)}</div>` : ''
    return `<tr><td class="num">${i+1}</td><td class="mono" style="font-size:11px">${esc(tk.query || '')}</td>
      <td class="num">${dur}</td><td class="num">${tk.tool_calls}</td><td class="num">${fmtTok(tk.tokens?.input)}</td>
      <td class="num">${fmtTok(tk.tokens?.output)}</td></tr>
      ${subs.length || loose.length ? `<tr><td colspan="6"><div class="subs-block">${subsHtml}${looseHtml}</div></td></tr>` : ''}`
  }).join('')
  return `<div class="panel"><h2>任务列表（按用户对话）· ${tasks.length}</h2><table>
    <thead><tr><th>#</th><th>任务</th><th class="num">耗时</th><th class="num">工具</th><th class="num">in</th><th class="num">out</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`
}

function trajectoryHtml(events) {
  const model = deriveModel(events)
  const rows = deriveRows(events, model)
  if (!model) return '<div class="panel"><h2>轨迹</h2><div class="muted">无事件</div></div>'
  const proj = buildTimeProjection(model.records)
  const v0 = proj.map(model.t0), v1 = proj.map(model.t1)
  const span = Math.max(1, v1 - v0)
  const rel = t => ((proj.map(t) - v0) / span * 100).toFixed(2)
  const lanesHtml = LANE_NAMES.map((name, li) => {
    const segs = model.lanes[li].map(rec => {
      const left = rel(rec.start)
      const width = Math.max(0.6, (proj.map(rec.end) - proj.map(rec.start)) / span * 100).toFixed(2)
      const color = rec.isError ? '#cf222e' : (KIND_COLOR[rec.kind] || '#58a6ff')
      const isAsk = rec.kind === 'ask'
      const isWait = rec.kind === 'input-wait'
      const isUser = rec.kind === 'input-user'
      const isMsg = rec.kind === 'input-user' || rec.kind === 'input-sys' || rec.kind === 'model'
      const compressed = isWait && proj.compressedStarts.has(rec.start)
      let label = rec.label.slice(0, 22)
      if (isWait && rec.dur != null) label = `等待 ${fmtDurC(rec.dur)}${compressed ? ' ≈' : ''}`
      else if (isAsk && rec.dur != null) label = `等 ${(rec.dur/1000).toFixed(1)}s`
      else if (rec.unresolved) label = label + ' 未返'
      const cls = [isWait ? 'wait' : '', isAsk ? 'ask' : '', isUser ? 'user' : '', compressed ? 'compressed' : '', isMsg ? 'msg' : '', rec.unresolved ? 'unresolved' : ''].filter(Boolean).join(' ')
      return `<div class="tl-seg ${cls}" style="left:${left}%;width:${width}%;background:${color}"
               title="${esc(rec.unresolved ? rec.label + ' · 未返回（无 tool/result，执行时间已延伸覆盖）' : (isWait && rec.dur != null ? '跨轮次等待（用户未响应）'+fmtDur(rec.dur)+(compressed ? ' · 时间线已压缩显示' : '') : rec.label))}">
        ${!isMsg && parseFloat(width) > 5 ? `<span class="tl-seg-label">${esc(label)}</span>` : ''}</div>`
    }).join('')
    return `<div class="tl-lane"><div class="tl-lane-label">${name}</div><div class="tl-lane-body">${segs}</div></div>`
  }).join('')
  const axis = [0, 0.25, 0.5, 0.75, 1].map(p =>
    `<span class="tl-tick" style="left:${p*100}%">${fmtDurC(proj.inv(v0 + span*p) - model.t0)}</span>`).join('')
  const ledger = rows.map((r, i) => {
    if (r.kind === 'turn') return `<tr><td colspan="5" style="border-top:1px dashed var(--border);color:var(--ink3);font-size:10px">turn ${r.turn} · ${esc(r.content)}</td></tr>`
    if (r.kind === 'tool') {
      const pt = r.status === 'ok' ? 'ok' : r.status === 'fail' ? 'bad' : r.status === 'ask' ? 'ask' : 'run'
      const label = r.status === 'ok' ? 'ok' : r.status === 'fail' ? 'fail' : r.status === 'ask' ? '等输入' : ''
      const durCell = r.status === 'ask' && r.dur != null
        ? `<span class="dur-wait">${fmtDur(r.dur)}</span>` : (r.dur != null ? r.dur+'ms' : '')
      return `<tr class="${r.status === 'ask' ? 'askrow' : ''}"><td title="${r.seq != null ? '原始行号 #' + r.seq : ''}">${r.no}</td><td class="ktype">${esc(r.name)}</td><td class="content" title="${esc(r.content)}">${esc(r.content)}</td><td class="dur">${durCell}</td><td><span class="pt ${pt}"></span>${label}</td></tr>`
    }
    return `<tr><td title="${r.seq != null ? '原始行号 #' + r.seq : ''}">${r.no}</td><td class="ktype">${r.type}</td><td class="content" title="${esc(r.content)}">${esc(r.content)}</td><td class="dur"></td><td></td></tr>`
  }).join('')
  return `<div class="panel"><h2>轨迹</h2>
    <div class="tl-canvas"><div class="tl-axis">${axis}</div>${lanesHtml}</div>
    <table class="ledger" style="margin-top:8px"><thead><tr><th>#</th><th>类型</th><th>内容</th><th>耗时</th><th>状态</th></tr></thead><tbody>${ledger}</tbody></table>
    </div>`
}

export function buildReportHtml(view) {
  const m = view.metrics || {}
  const sid = view._sid || 'session'
  const title = `单会话评测报告 · ${sid}`
  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${esc(title)}</title><style>${CSS}</style></head>
<body><div class="wrap">
  <div class="hero">
    <h1>单会话评测报告</h1>
    <div class="sub">${esc(sid)}</div>
    <div class="hero-grid">
      <div class="hitem"><div class="v">${m.user_turns ?? '—'}</div><div class="k">轮次（用户指令数）</div></div>
      <div class="hitem"><div class="v">${fmtDur(m.duration_ms_official != null ? m.duration_ms_official : m.duration_ms)}</div><div class="k">总耗时</div></div>
      <div class="hitem"><div class="v">${m.skill_loaded || '—'}</div><div class="k">Skill</div></div>
      <div class="hitem"><div class="v">${fmtTok((m.input_tokens||0)+(m.cache_read_tokens||0)+(m.output_tokens||0))}</div><div class="k">总 Token</div></div>
      <div class="hitem"><div class="v">${m.tool_calls_total ?? '—'}</div><div class="k">工具调用总数</div></div>
      <div class="hitem"><div class="v">${m.cost_cny != null ? '¥'+m.cost_cny.toFixed(4) : (m.cost_est_cny != null ? '~¥'+m.cost_est_cny.toFixed(4) : '—')}</div><div class="k">成本</div></div>
    </div>
    ${statstripHtml(m)}
  </div>
  ${reportHtml(m)}
  ${tasksHtml(m)}
  ${trajectoryHtml(view.events || [])}
  ${view.raw ? `<div class="panel"><h2>原始日志</h2><pre class="rawlog">${esc(view.raw)}</pre></div>` : ''}
  ${view.result ? `<div class="final">${[
    `session: ${view.result.session_id}`,
    `finish_reason: ${view.result.finish_reason}`,
    `log: ${view.result.log_path}`,
  ].filter(Boolean).join('\n')}</div>` : ''}
</div></body></html>`
}
