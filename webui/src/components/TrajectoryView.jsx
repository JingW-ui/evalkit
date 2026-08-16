import React, { useMemo, useRef, useState } from 'react'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtMs = ms => ms == null ? '—' : ms >= 3600000 ? (ms/3600000).toFixed(1)+'h'
  : ms >= 60000 ? (ms/60000).toFixed(1)+'m' : ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms'
const fmtTok = n => n == null ? '—' : n.toLocaleString()
const fmtTime = ms => ms ? new Date(ms).toLocaleTimeString() : '—'

// 断裂时间轴投影：超长跨轮次等待（>compressAfter）在时间线上压缩到 capMs 等效宽度，
// 活动区保持真实比例，把横向空间让给工具/模型细节。返回 { map, inv, segs, compressedStarts }。
// map: 真实时间→显示坐标；inv: 显示坐标→真实时间（拖拽缩放反算用）。
export function buildTimeProjection(records, compressAfter = 60000, capMs = 60000) {
  const segs = []
  for (const r of records) {
    if (r.kind === 'input-wait' && r.dur != null && r.dur > compressAfter) segs.push({ a: r.start, b: r.end })
  }
  segs.sort((x, y) => x.a - y.a)
  const merged = []
  for (const s of segs) {
    const last = merged[merged.length - 1]
    if (last && s.a <= last.b) last.b = Math.max(last.b, s.b)
    else merged.push({ ...s })
  }
  if (!merged.length) return { map: t => t, inv: t => t, segs: merged, compressedStarts: new Set() }
  function map(t) {
    let shift = 0
    for (const s of merged) {
      if (t <= s.a) break
      if (t < s.b) return s.a - shift + (t - s.a) * capMs / (s.b - s.a)
      shift += (s.b - s.a) - capMs
    }
    return t - shift
  }
  function inv(y) {
    let shift = 0
    for (const s of merged) {
      const dispA = s.a - shift, dispB = dispA + capMs
      if (y <= dispA) break
      if (y < dispB) return s.a + (y - dispA) * (s.b - s.a) / capMs
      shift += (s.b - s.a) - capMs
    }
    return y + shift
  }
  return { map, inv, segs: merged, compressedStarts: new Set(merged.map(s => s.a)) }
}

const LANE_NAMES = ['input', 'model', 'tools/mcp/skill']
// 泳道配色（对齐 DSH：user=蓝、message=紫调、tool=黄橙）——三泳道整体色调分离，区分度优先：
// input 泳道 蓝/灰/深灰 · model 泳道 紫 · tools 泳道 绿/青/橙/黄
const KIND_COLOR = { input: '#6e7681', input_user: '#58a6ff', input_sys: '#6e7681',
                     input_wait: '#30363d',
                     model: '#bc8cff', tool: '#2da44e', mcp: '#39c5cf', skill: '#f0883e', ask: '#d29922' }
const ASK_TOOLS = new Set(['AskUserQuestion', 'ask_user_question', 'question'])

// ---------- 数据派生（事件流 → 三泳道时间线记录 + 台账行，统一按事件序号 eidx 关联；导出复用） ----------
// 对齐 DSH deriveTimedTimeline：span=[startedAt, startedAt+duration]。
// 时间线被三类活动完全覆盖、无缝：用户输入=零宽点、assistant 消息=模型思考块
// [上一活动结束, 消息到达]（DSH message startedAt=stepStartTime/prevAbsTime）、
// 工具=真实区间（call→result）、跨轮次等待=真实区间（投影压缩）。
export function deriveModel(events) {
  const lanes = [[], [], []]
  const records = []
  const pending = new Map()
  let lastEvtAt = null      // 上一条非 user 事件时间（跨轮次等待检测）
  let lastActivityAt = null // 上一条活动结束时间（user 到达 / tool/result / assistant 消息）
  let prevTime = null       // 前一事件时间（缺 time 的事件继承它，绝不用回放时的当前时间）
  events.forEach((e, eidx) => {
    // 缺 time 的事件（如 Claude init 行）继承前一事件时间，避免 Date.now() 兜底成回放时刻拉爆时间线
    const time = e.time != null ? e.time : (prevTime != null ? prevTime : 0)
    if (e.time != null) prevTime = time
    const t = e.type, d = e.data || {}
    if (t === 'tool/call') {
      const rec = { id: records.length, eidx, seq: e.seq != null ? e.seq : eidx,
                    lane: 2, start: time, end: null, dur: null,
                    name: d.name || 'tool', label: d.name || 'tool', isError: false,
                    kind: d.name === 'skill' || d.name === 'Skill' ? 'skill'
                      : ASK_TOOLS.has(d.name) ? 'ask'
                      : String(d.name || '').startsWith('mcp__') ? 'mcp' : 'tool',
                    callId: d.callId, detail: { arguments: d.arguments || '' } }
      if (rec.kind === 'mcp') rec.label = d.name.replace('mcp__', '')
      pending.set(d.callId, rec)
      records.push(rec); lanes[2].push(rec)
      if (lastEvtAt == null || time > lastEvtAt) lastEvtAt = time
    } else if (t === 'tool/result') {
      const rec = pending.get(d.callId)
      if (!rec) return
      rec.end = time; rec.dur = Math.max(1, time - rec.start)
      rec.isError = d.message?.content?.[0]?.isError === true
      rec.detail.result = d.message?.content?.[0]?.content ?? ''
      lastActivityAt = time   // 工具结束，模型开始处理结果（下一消息块从此刻起）
      if (lastEvtAt == null || time > lastEvtAt) lastEvtAt = time
    } else if (t === 'user/message' || t === 'request/header') {
      const isUser = t === 'user/message'
      // skill_listing（系统注入技能列表）：不建独立点，并入上一条真实用户输入记录，
      // 点击用户 input 时在详情面板展示（detail.skills）
      if (isUser && d.system_injected && d.skills) {
        let prev = null
        for (let i = records.length - 1; i >= 0; i--) {
          if (records[i].kind === 'input-user') { prev = records[i]; break }
        }
        if (prev) {
          prev.detail.skills = d.skills
          prev.skillCount = d.skills.length
          prev.label = `${prev.label} · 技能 ${d.skills.length}`
        }
        return
      }
      const text = isUser
        ? (d.content || []).filter(b => b && b.type === 'text').map(b => b.text || '').join('').slice(0, 300) || '(输入)'
        : `${(d.header?.tools || []).length} tools · ${d.header?.model || ''}`
      // 跨轮次等待：上一条非 user 事件 → 本次用户输入之间的长间隙（>30s）并入 input 泳道
      if (isUser && lastEvtAt != null && time - lastEvtAt > 30000) {
        const wait = { id: records.length, eidx: -1, seq: '·',
                       lane: 0, start: lastEvtAt, end: time, dur: time - lastEvtAt,
                       name: 'wait', label: '', isError: false,
                       kind: 'input-wait', detail: {} }
        records.push(wait); lanes[0].push(wait)
      }
      const rec = { id: records.length, eidx, seq: e.seq != null ? e.seq : eidx,
                    lane: 0, start: time, end: time, dur: 1,   // 用户输入零宽点（DSH 口径）
                    name: isUser ? 'user' : 'system',
                    label: text,
                    isError: false,
                    kind: isUser ? 'input-user' : 'input-sys',
                    detail: { text, skills: undefined } }
      records.push(rec); lanes[0].push(rec)
      if (isUser) {
        lastEvtAt = null            // 用户输入到达，等待结束
        lastActivityAt = time       // 模型开始处理输入（消息块从此刻起）
      } else if (lastEvtAt == null || time > lastEvtAt) lastEvtAt = time
    } else if (t === 'assistant/message') {
      const blocks = d.message?.content || []
      const text = blocks.filter(b => b.type === 'text').map(b => b.text || '').join('').slice(0, 300)
      const thinking = blocks.filter(b => b.type === 'reasoning' || b.type === 'thinking')
        .map(b => b.text || '').join('').slice(0, 1200)
      // 模型思考块（DSH 口径）：start=上一活动结束（user/工具 result/上一条消息），
      // end=本条消息到达 → 思考时段被覆盖，与工具块首尾相接、时间线无缝。
      // 若下一条事件是同轮 tool/call（模型同一段输出的调用发出），end 延伸覆盖到调用时刻。
      const start = lastActivityAt != null ? lastActivityAt : time
      const nextEv = events[eidx + 1]
      const end = (nextEv && nextEv.type === 'tool/call' && nextEv.time != null && nextEv.time >= time)
        ? nextEv.time : time
      const rec = { id: records.length, eidx, seq: e.seq != null ? e.seq : eidx,
                    lane: 1, start, end, dur: Math.max(1, end - start),
                    name: 'model',
                    label: text || (blocks.length && blocks.every(b => b.type === 'tool_use') ? '(调用工具)' : '(assistant)'),
                    isError: false,
                    kind: 'model',
                    detail: { text, thinking: thinking || undefined, usage: d.message?.usage } }
      records.push(rec); lanes[1].push(rec)
      lastActivityAt = end   // 消息完成（若紧跟 tool/call，工具块从此刻起，首尾相接）
      if (lastEvtAt == null || time > lastEvtAt) lastEvtAt = time
    }
  })
  if (!records.length) return null
  // 未闭合工具调用（无 tool/result，如会话中断/并行调用 result 缺失）：
  // 延伸到下一个同轮活动事件覆盖执行时间；若直接跨轮次 user 或已到末尾，退化为点（未返回标记）
  for (const rec of records) {
    if (rec.lane === 2 && rec.end == null) {
      let next = null, nextUser = null
      for (let i = rec.eidx + 1; i < events.length; i++) {
        const t = events[i].type
        if (t === 'user/message' && nextUser == null) nextUser = events[i]
        else if (t !== 'user/message' && events[i].time != null) { next = events[i]; break }
      }
      if (next && (!nextUser || (next.time || 0) <= (nextUser.time || Infinity))) {
        rec.end = next.time; rec.dur = Math.max(1, rec.end - rec.start)
        rec.unresolved = true   // 有下一个活动事件：块延伸，标注未返回
      } else {
        rec.end = rec.start; rec.dur = 1   // 跨轮次/末尾：退化为点
      }
    }
  }
  const t0 = Math.min(...records.map(r => r.start))
  const t1 = Math.max(...records.map(r => r.end))
  return { t0, t1, lanes, records, byEidx: new Map(records.map(r => [r.eidx, r])) }
}

export function deriveRows(events, model) {
  const out = []
  const pending = new Map()
  let turn = 0
  let no = 0   // 连续事件序号（台账 # 列；原始日志行号跳跃不美观）
  // 跨轮次等待不注入台账行：时间线 input 泳道灰块已表达（置顶注入会刷屏且无可点击详情）。
  events.forEach((e, eidx) => {
    const t = e.type, d = e.data || {}, time = e.time || Date.now()
    const seq = e.seq != null && e.seq >= 0 ? e.seq : '·'
    if (t === 'turn/end') { turn++; out.push({ kind: 'turn', turn, content: 'reason: ' + (d.reason?.kind || '') }); return }
    if (t === 'tool/call') {
      let args = ''; try { args = JSON.stringify(JSON.parse(d.arguments || '{}')).slice(0, 90) } catch {}
      const row = { kind: 'tool', eidx, seq, no: ++no, name: d.name || 'tool', content: args, dur: null, status: 'run', callId: d.callId, time }
      if (ASK_TOOLS.has(row.name)) row.status = 'ask'   // 等待人为输入
      pending.set(d.callId, out.push(row) - 1)
      return
    }
    if (t === 'tool/result') {
      const idx = pending.get(d.callId)
      if (idx == null) return
      pending.delete(d.callId)
      const row = out[idx]
      const err = d.message?.content?.[0]?.isError === true
      row.dur = Math.max(0, time - row.time)
      row.status = err ? 'fail' : 'ok'
      return
    }
    let content = ''
    if (t === 'user/message') {
      // skill_listing 不产台账行（已并入前一条 user 记录）
      if (d.system_injected && d.skills) return
      content = (d.content || []).filter(b => b && b.type === 'text').map(b => b.text || '').join('').slice(0, 160)
    }
    else if (t === 'assistant/message') content = (d.message?.content || []).map(b => b.text || '').join('').slice(0, 180)
    else if (t === 'request/header') content = `${(d.header?.tools || []).length} tools · ${d.header?.model || ''}`
    else return
    out.push({ kind: 'msg', eidx, type: t === 'user/message' ? 'user' : 'assistant', seq, no: ++no, content })
  })
  return out
}

// ---------- 一体化轨迹视图：时间线 overview（上） + 台账（下），联动详情（onInspect 交给 App 全局面板） ----------
export default function TrajectoryView({ events, onInspect }) {
  const model = useMemo(() => deriveModel(events), [events])
  const rows = useMemo(() => deriveRows(events, model), [events, model])
  const proj = useMemo(() => buildTimeProjection(model ? model.records : []), [model])
  const [range, setRange] = useState(null)
  const [selected, setSelected] = useState(null)
  const [collapsedTurns, setCollapsedTurns] = useState(new Set())
  const [collapsedUsers, setCollapsedUsers] = useState(new Set())
  const boxRef = useRef(null)
  const tableRef = useRef(null)
  const dragRef = useRef(null)

  // 选中联动：高亮 + 通知全局面板（onInspect）
  function selectByEidx(eidx) {
    if (eidx == null || !model) return
    const rec = model.byEidx.get(eidx)
    if (!rec) return
    setSelected(rec)
    onInspect && onInspect(rec)
    tableRef.current?.querySelector(`[data-eidx="${eidx}"]`)?.scrollIntoView({ block: 'nearest' })
  }

  if (!model) return <div className="muted empty">等待事件…</div>

  // 全部坐标走断裂投影：超长等待压缩，活动区真实比例
  const viewStart = proj.map(range ? range.start : model.t0)
  const viewEnd = proj.map(range ? range.end : model.t1)
  const span = Math.max(1, viewEnd - viewStart)
  const rel = t => ((proj.map(t) - viewStart) / span * 100)

  const onDown = e => { const rect = boxRef.current.getBoundingClientRect(); dragRef.current = { x0: e.clientX, rect } }
  const onMove = e => { if (dragRef.current) dragRef.current.x1 = e.clientX }
  const onUp = () => {
    const drag = dragRef.current; dragRef.current = null
    if (!drag || drag.x1 == null) return
    const { rect } = drag
    const x0 = Math.min(drag.x0, drag.x1), x1 = Math.max(drag.x0, drag.x1)
    if (x1 - x0 < 4) return
    const toT = px => proj.inv(viewStart + (px - rect.left) / rect.width * span)
    setRange({ start: toT(x0), end: toT(x1) })
  }

  const toggleTurn = turn => { const n = new Set(collapsedTurns); n.has(turn) ? n.delete(turn) : n.add(turn); setCollapsedTurns(n) }
  const toggleUser = eidx => { const n = new Set(collapsedUsers); n.has(eidx) ? n.delete(eidx) : n.add(eidx); setCollapsedUsers(n) }

  return (
    <div className="tv">
      <div className="tv-toolbar">
        <span className="muted" style={{ fontSize: 11 }}>
          {model.records.length} 条记录 · 全范围 {fmtMs(model.t1 - model.t0)}
          {proj.segs.length > 0 && <span className="tl-compressed-tip"> · 压缩 {proj.segs.length} 段等待</span>}
          {range && <span style={{ color: 'var(--blue)' }}> · 已缩放 {fmtMs(proj.inv(range.end) - proj.inv(range.start))}</span>}
        </span>
        <span style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
          <button className="ghost" onClick={() => setRange(null)}>重置</button>
          <span className="muted" style={{ fontSize: 10 }}>拖拽选区间 · 点击记录看详情</span>
        </span>
      </div>

      {/* 时间线 overview（三泳道） */}
      <div className="tl-canvas" ref={boxRef} onMouseDown={onDown} onMouseMove={onMove} onMouseUp={onUp}
           onDoubleClick={() => setRange(null)}>
        <div className="tl-axis">
          {[0, 0.25, 0.5, 0.75, 1].map(p => (
            <span key={p} className="tl-tick" style={{ left: p * 100 + '%' }}>{fmtMs(proj.inv(viewStart + span * p) - model.t0)}</span>
          ))}
        </div>
        {LANE_NAMES.map((name, li) => (
          <div className="tl-lane" key={name}>
            <div className="tl-lane-label">{name}</div>
            <div className="tl-lane-body">
              {model.lanes[li].map(rec => {
                const left = rel(rec.start)
                const width = Math.max(0.6, (proj.map(rec.end) - proj.map(rec.start)) / span * 100)
                const color = rec.isError ? '#cf222e' : (KIND_COLOR[rec.kind] || '#58a6ff')
                const isAsk = rec.kind === 'ask'
                const isWait = rec.kind === 'input-wait'
                const isUser = rec.kind === 'input-user'
                const isMsg = rec.kind === 'input-user' || rec.kind === 'input-sys' || rec.kind === 'model'
                const compressed = isWait && proj.compressedStarts.has(rec.start)
                // 标签：等待段显示「等待 Xs」；ask 段「等 Xs」；消息点不显示文字（hover/点击看详情，与工具泳道一致）
                let label = rec.label.slice(0, 22)
                if (isWait && rec.dur != null) label = `等待 ${fmtMs(rec.dur)}${compressed ? ' ≈' : ''}`
                else if (isAsk && rec.dur != null) label = `等 ${(rec.dur / 1000).toFixed(1)}s`
                else if (rec.unresolved) label = label + ' 未返'
                return (
                  <div key={rec.id} className={`tl-seg ${isWait ? 'wait' : isAsk ? 'ask' : isUser ? 'user' : ''} ${compressed ? 'compressed' : ''} ${isMsg ? 'msg' : ''} ${rec.unresolved ? 'unresolved' : ''} ${selected?.id === rec.id ? 'sel' : ''}`}
                       style={{ left: left + '%', width: width + '%', background: color }}
                       title={rec.unresolved ? `${rec.label} · 未返回（无 tool/result，执行时间已延伸覆盖）` : (isWait && rec.dur != null ? `跨轮次等待（用户未响应）${fmtMs(rec.dur)}${compressed ? ' · 时间线已压缩显示' : ''}` : rec.label)}
                       onClick={e => { if (rec.eidx >= 0) { e.stopPropagation(); selectByEidx(rec.eidx) } }}>
                    {!isMsg && width > 5 ? <span className="tl-seg-label">{esc(label)}</span> : null}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      {/* 台账（与时间线一体，点击行联动详情） */}
      <div className="ledger-wrap" ref={tableRef}>
        <table className="ledger">
          <thead><tr><th>#</th><th>类型</th><th>内容</th><th>耗时</th><th>状态</th></tr></thead>
          <tbody>
            {rows.map((r, i) => {
              if (r.kind === 'turn') {
                const collapsed = collapsedTurns.has(r.turn)
                return (
                  <tr key={i} className="turnrow" onClick={() => toggleTurn(r.turn)}>
                    <td colSpan="5">turn {r.turn} · {collapsed ? '已折叠' : r.content} {collapsed ? '▸' : '▾'}</td>
                  </tr>
                )
              }
              if (r.kind === 'tool') {
                const pt = r.status === 'ok' ? 'ok' : r.status === 'fail' ? 'bad' : r.status === 'ask' ? 'ask' : 'run'
                const label = r.status === 'ok' ? 'ok' : r.status === 'fail' ? 'fail' : r.status === 'ask' ? '等输入' : ''
                // 等待输入行：耗时列显示等待时长（AskUserQuestion 挂起）
                const durCell = r.status === 'ask' && r.dur != null
                  ? <span className="dur-wait">{fmtMs(r.dur)}</span>
                  : (r.dur != null ? r.dur + 'ms' : '')
                return (
                  <tr key={i} data-eidx={r.eidx} className={`${r.status === 'ask' ? 'askrow' : ''} ${selected?.eidx === r.eidx ? 'sel' : ''}`}
                      onClick={() => selectByEidx(r.eidx)}>
                    <td className="seq" title={r.seq != null ? '原始行号 #' + r.seq : ''}>{r.no}</td>
                    <td className="ktype">{r.name}</td>
                    <td className="content" title={r.content}>{r.content}</td>
                    <td className="dur">{durCell}</td>
                    <td className="status"><span className="st"><span className={`pt ${pt}`} />{label}</span></td>
                  </tr>
                )
              }
              const userCollapsed = r.type === 'user' && collapsedUsers.has(r.eidx)
              return (
                <tr key={i} data-eidx={r.eidx} className={`${r.type === 'user' ? 'userrow' : ''} ${selected?.eidx === r.eidx ? 'sel' : ''}`}
                    onClick={() => r.type === 'user' ? selectByEidx(r.eidx) : selectByEidx(r.eidx)}
                    title={r.type === 'user' ? '点击看详情（含技能列表等）' : undefined}>
                  <td className="seq" title={r.seq != null ? '原始行号 #' + r.seq : ''}>{r.no}</td>
                  <td className="ktype">
                    {r.type}
                    {r.type === 'user' && (
                      <button className="ghost collapse-btn" title="收起/展开内容"
                              onClick={e => { e.stopPropagation(); toggleUser(r.eidx) }}>
                        {userCollapsed ? '▸' : '▾'}
                      </button>
                    )}
                  </td>
                  <td className="content" title={r.content}>{userCollapsed ? '(已收起)' : r.content}</td>
                  <td className="dur" />
                  <td className="status" />
                </tr>
              )
            })}
            {!rows.length && <tr><td className="empty" colSpan="5" style={{ padding: 12 }}>等待事件…</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}
