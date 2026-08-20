import React, { useState } from 'react'

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))
const fmtTok = n => n == null ? '—' : (n >= 100000000 ? (n/100000000).toFixed(2)+'亿' : n >= 10000 ? (n/10000).toFixed(1)+'万' : n.toLocaleString())
const fmtT = ms => ms ? new Date(ms).toLocaleString() : '—'
const SUB_ICON = { completed: '✓', in_progress: '↻', blocked: '✕', pending: '·' }
const SUB_CLS = { completed: 'ok', in_progress: 'run', blocked: 'bad', pending: 'muted' }
const SUB_LABEL = { completed: '完成', in_progress: '进行中', blocked: '阻塞', pending: '待处理' }

// 子任务 → 工具归属：按时间窗 [created, updated] 归集（updated = TaskUpdate 完成时间）。
// 注意不能用 next.created_ms 作上界——工具发生在各自子任务的 TaskUpdate(completed) 之前，
// 而 TaskCreate 是连续快速创建的（三个 created 只差几秒），next.created 会截断窗口导致漏采。
// 退化窗口（updated<=created 或缺失 = 未完成/规划中子任务）不参与归集，其工具归任务级，
// 避免 Infinity 窗口吞掉后续所有工具。
function linkSubTools(tk) {
  const tools = tk.tools || []
  const subs = (tk.subitems || []).map(s => ({ ...s, tools: [], _valid: false }))
  for (let i = 0; i < subs.length; i++) {
    const s = subs[i]
    if (s.created_ms == null) continue
    const hi = s.updated_ms != null && s.updated_ms > s.created_ms ? s.updated_ms : null
    if (hi == null) continue   // 未完成/退化 → 不参与
    s._window = [s.created_ms, hi]
    s._valid = true
  }
  const loose = []
  for (const tc of tools) {
    // 书签工具（TaskCreate/TaskUpdate 自身）不展示，仅作窗口边界
    if (tc.name === 'TaskCreate' || tc.name === 'TaskUpdate') continue
    const t = tc.call_ms
    let placed = null
    for (const s of subs) {
      if (s._valid && t >= s._window[0] && t <= s._window[1]) { placed = s; break }
    }
    if (placed) placed.tools.push(tc)
    else loose.push(tc)
  }
  return { subs, loose }
}

// 工具链箭头：连接线 + 箭头头的 SVG（按下一节点状态着色）
function FlowArrow({ state }) {
  const cls = state === 'ok' ? 'ok' : state === 'bad' ? 'bad' : ''
  return (
    <span className={`tc-arrow ${cls}`}>
      <svg width="14" height="8" viewBox="0 0 14 8" fill="none">
        <path d="M0 4h11" stroke="currentColor" strokeWidth="1.2" />
        <path d="M11 1l3 3-3 3z" fill="currentColor" />
      </svg>
    </span>
  )
}

// 工具节点（点击 → 全局面板详情）
function ToolNode({ tc, onInspect }) {
  const cls = tc.ok === false ? 'bad' : tc.ok === true ? 'ok' : ''
  return (
    <span className={`tc-node ${cls}`} title={tc.args || tc.name}
          onClick={() => onInspect && onInspect({
            kind: 'tool', name: tc.name, isError: tc.ok === false,
            dur: tc.dur_ms, start: tc.call_ms, seq: null,
            callId: tc.callId,
            detail: { arguments: tc.args, result: tc.result,
                      args_truncated: tc.args_truncated, result_truncated: tc.result_truncated },
          })}>
      <span className="tc-name">{esc(tc.name)}</span>
      {tc.dur_ms != null && <span className="tc-dur">{tc.dur_ms}ms</span>}
      {tc.ok === false && <span className="tc-x">✗</span>}
      {tc.ok === true && <span className="tc-ok">✓</span>}
    </span>
  )
}

function ToolChain({ tools, onInspect }) {
  if (!tools || !tools.length) return null
  return (
    <div className="toolchain">
      {tools.map((tc, j) => (
        <React.Fragment key={j}>
          {j > 0 && <FlowArrow state={tc.ok === false ? 'bad' : tc.ok === true ? 'ok' : ''} />}
          <ToolNode tc={tc} onInspect={onInspect} />
        </React.Fragment>
      ))}
    </div>
  )
}

// 任务列表：子任务与工具链结合，两级显示（摘要 = 子任务行；详情 = 点击展开该子任务的工具链）
export default function TasksPanel({ metrics, onInspect }) {
  const m = metrics || {}
  const tasks = m.tasks || []
  const [collapsedTasks, setCollapsedTasks] = useState(new Set())   // 整个任务块收起
  const [openSubs, setOpenSubs] = useState(new Set())               // 已展开工具链的子任务
  if (!tasks.length) return <div className="muted empty" style={{ padding: 16 }}>无任务数据（事件流未切分到任务）</div>

  const toggleTask = i => { const n = new Set(collapsedTasks); n.has(i) ? n.delete(i) : n.add(i); setCollapsedTasks(n) }
  const toggleSub = key => { const n = new Set(openSubs); n.has(key) ? n.delete(key) : n.add(key); setOpenSubs(n) }

  return (
    <div className="panel">
      <h2>任务列表（按用户对话）· {tasks.length}</h2>
      <table>
        <thead><tr><th>#</th><th>任务</th><th>开始</th><th>结束</th><th className="num">耗时</th><th className="num">工具</th><th className="num">in</th><th className="num">out</th></tr></thead>
        <tbody>
          {tasks.map((tk, i) => {
            const dur = tk.end_ms && tk.start_ms ? ((tk.end_ms - tk.start_ms) / 1000).toFixed(0) + 's' : '—'
            const { subs, loose } = linkSubTools(tk)
            const collapsed = collapsedTasks.has(i)
            return (
              <React.Fragment key={i}>
                <tr className={`taskrow ${collapsed ? 'collapsed' : ''}`} onClick={() => toggleTask(i)}>
                  <td className="num">{i + 1}</td>
                  <td className="tq" title={tk.query}>
                    <span className={`task-chev ${collapsed ? '' : 'open'}`}>▸</span>
                    {esc(tk.query || '')}
                  </td>
                  <td className="num muted">{tk.start_ms ? fmtT(tk.start_ms).slice(5, 16) : '—'}</td>
                  <td className="num muted">{tk.end_ms ? fmtT(tk.end_ms).slice(5, 16) : '—'}</td>
                  <td className="num">{dur}</td>
                  <td className="num">{tk.tool_calls}</td>
                  <td className="num">{fmtTok(tk.tokens?.input)}</td>
                  <td className="num">{fmtTok(tk.tokens?.output)}</td>
                </tr>
                {!collapsed && (
                  <tr className="subrow">
                    <td colSpan="8">
                      {/* 子任务区：摘要（默认收起时只显示头） */}
                      {subs.length > 0 && (
                        <div className="subs-block">
                          <div className="subs-head">
                            <span className="subs-lbl">子任务 {subs.length}</span>
                            <span className="muted" style={{ fontSize: 10 }}>点击子任务展开其工具链过程</span>
                          </div>
                          {subs.map((s, j) => {
                            const key = i + ':' + j
                            const open = openSubs.has(key)
                            return (
                              <div className={`subtask ${open ? 'open' : ''}`} key={key}>
                                <div className="subtask-summary" onClick={() => toggleSub(key)}>
                                  <span className={`sub-chev ${open ? 'open' : ''}`}>▸</span>
                                  <span className={`sub-ic ${SUB_CLS[s.status] || 'muted'}`}>{SUB_ICON[s.status] || '·'}</span>
                                  <span className="subj" title={s.description || s.subject}>
                                    {esc(s.subject || s.description || s.status)}
                                  </span>
                                  <span className={`subst ${SUB_CLS[s.status] || ''}`}>{SUB_LABEL[s.status] || s.status}</span>
                                  {s.tools.length > 0 && <span className="sub-tools">{s.tools.length} 工具</span>}
                                  <span className="subdur muted">
                                    {s.created_ms ? fmtT(s.created_ms).slice(5, 16) : ''}
                                  </span>
                                </div>
                                {/* 详情：该子任务的工具链过程 */}
                                {open && (
                                  <div className="subtask-detail">
                                    <ToolChain tools={s.tools} onInspect={onInspect} />
                                    {!s.tools.length && <span className="muted" style={{ fontSize: 10 }}>该子任务无工具调用</span>}
                                  </div>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                      {/* 任务级工具链（未归属任何子任务） */}
                      {loose.length > 0 && (
                        <div className="subs-block">
                          <div className="subs-head">
                            <span className="subs-lbl">任务级工具链</span>
                            <span className="muted" style={{ fontSize: 10 }}>{loose.length} 次调用</span>
                          </div>
                          <ToolChain tools={loose} onInspect={onInspect} />
                        </div>
                      )}
                      {!subs.length && !loose.length && (
                        <span className="muted" style={{ fontSize: 11 }}>无子任务 / 工具调用</span>
                      )}
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
