import React, { useEffect, useRef, useState } from 'react'
import { getSessions, getAgentStatus, attach, detach, addSessionPath, getRaw, connectSSE } from './api.js'
import SessionList from './components/SessionList.jsx'
import Report from './components/Report.jsx'
import TasksPanel from './components/TasksPanel.jsx'
import TrajectoryView from './components/TrajectoryView.jsx'
import RawLog from './components/RawLog.jsx'
import EvalMatrix from './components/EvalMatrix.jsx'
import StatsPanel from './components/StatsPanel.jsx'
import BatchEval from './components/BatchEval.jsx'
import TaskBank from './components/TaskBank.jsx'
import Inspector from './components/Inspector.jsx'
import { buildReportHtml } from './exportHtml.js'

const initialView = () => ({
  events: [], metrics: null, result: null, warnings: [],
  status: null, statusCls: '', progress: null, raw: null, rawLoading: false,
})

const fmtTok = n => n == null ? '—' : (n >= 100000000 ? (n/100000000).toFixed(2)+'亿' : n >= 10000 ? (n/10000).toFixed(1)+'万' : n.toLocaleString())
const fmtDur = ms => { if (ms == null) return '—'; const s = ms/1000;
  if (s < 60) return s.toFixed(1)+'秒'; if (s < 3600) return Math.floor(s/60)+'分 '+Math.floor(s%60)+'秒';
  return Math.floor(s/3600)+'小时 '+Math.floor((s%3600)/60)+'分' }
const fmtDT = ms => ms ? new Date(ms).toLocaleString('zh-CN', { hour12: false }) : '—'

// 顶部高密度统计条（参考 DSH toolbar：duration/turns/calls 紧凑行）
function StatStrip({ m }) {
  m = m || {}
  const judge = m.judge || {}
  const taskVal = judge.success === undefined ? '—'
    : `${judge.success ? 'PASS' : 'FAIL'} · ${judge.level || '?'}`   // level 已含 L 前缀（如 L3）
  // 耗时拆分：整体 = duration；模型活跃 = llm_ms；等待输入 = human_wait_ms
  const overall = m.duration_ms_official != null ? m.duration_ms_official : m.duration_ms
  const llm = m.llm_ms ?? 0
  const wait = m.human_wait_ms ?? 0
  const waitPct = overall ? Math.round(wait / overall * 100) : null
  const items = [
    ['开始', fmtDT(m.started_at)],
    ['耗时', fmtDur(overall)],
    ['模型活跃', fmtDur(llm)],
    ['等输入', wait > 0 ? fmtDur(wait) + (waitPct != null ? ` (${waitPct}%)` : '') : '—'],
    ['轮次', m.user_turns ?? m.num_turns ?? '—'],          // 轮次=真实用户指令数（跨 agent 口径统一）
    ['任务', taskVal, judge.success === undefined ? '' : `判定: ${judge.by || judge.source || ''}`],
    ['工具', m.tool_calls_total ?? '—'],
    ['工具成功率', m.tool_success_rate == null ? '—' : (m.tool_success_rate*100).toFixed(0)+'%'],
    ['Token', fmtTok((m.input_tokens||0) + (m.cache_read_tokens||0) + (m.output_tokens||0))],
    ['in', fmtTok(m.input_tokens)],
    ['out', fmtTok(m.output_tokens)],
    ['cacheR', fmtTok(m.cache_read_tokens)],
    ['成本¥', m.cost_cny != null ? m.cost_cny.toFixed(3)
        : (m.cost_est_cny != null ? '~'+m.cost_est_cny.toFixed(3) : '—')],
    ['模型', m.model ? String(m.model).split('-')[0] : '—'],
    ['Skill', m.skill_loaded || '—'],
  ]
  return <div className="statstrip">{items.map(([k, v, sub]) => (
    <div className="ss-item" key={k}><span className="ss-k">{k}</span><span className="ss-v">{v}</span>
      {sub ? <span className="ss-sub">{sub}</span> : null}</div>
  ))}</div>
}

export default function App() {
  const [sessions, setSessions] = useState([])
  const [batchSessions, setBatchSessions] = useState([])   // 批量评测独立 tab 会话
  const [conn, setConn] = useState('连接中…')
  const [lastFrameAt, setLastFrameAt] = useState(0)   // 最近一次 SSE 帧时间（spinner 活跃判断）
  const [now, setNow] = useState(Date.now())           // 每秒 tick（驱动 spinner 过期重渲染）
  const [agentStatus, setAgentStatus] = useState({})   // agent 连接状态（claude/codemaker/dsh）
  const [cur, setCur] = useState(null)
  const [view, setView] = useState(initialView())
  const [tab, setTab] = useState('report')
  const [viewMode, setViewMode] = useState('session')   // session=会话评测 / batch=批量评测 / matrix=评测矩阵
  const [inspector, setInspector] = useState(null)       // 全局详情面板（轨迹/任务工具链触发）
  const viewRef = useRef(view)
  viewRef.current = view

  const refresh = () => {
    getSessions().then(setSessions).catch(() => {})
    getSessions('batch').then(setBatchSessions).catch(() => {})
  }

  const refreshStatus = () => getAgentStatus().then(setAgentStatus).catch(() => {})

  useEffect(() => {
    refresh()
    refreshStatus()
    const t = setInterval(refresh, 10000)
    const t2 = setInterval(refreshStatus, 15000)
    connectSSE(f => {
      if (f.session_id && f.session_id !== viewRef.current._sid) return
      handleFrame(f)
    }, s => setConn(s === 'connected' ? '已连接' : '断线重连…'))
    const t3 = setInterval(() => setNow(Date.now()), 1000)   // 每秒 tick：驱动 spinner 活跃判断过期
    return () => { clearInterval(t); clearInterval(t2); clearInterval(t3) }
  }, [])

  function handleFrame(f) {
    setLastFrameAt(Date.now())
    switch (f.type) {
      case 'run/start':
        setView(prev => ({
          ...initialView(),
          _sid: f.session_id,
          raw: prev.raw, rawLoading: prev.rawLoading,   // 保留原始日志（getRaw 已加载的）
          status: f.replay ? '重放中' : '运行中', statusCls: 'run',
          progress: f.replay ? { done: 0, total: f.total_events || 0 } : null,
        }))
        break
      case 'batch': {
        // 尾随批量帧：events 数组 + metrics 快照，一次 setState（避免逐事件高频重渲染）
        const evs = f.events || []
        setView(prev => {
          // 尾部截断：仅保留最近 MAX_EVENTS 条，避免运行中会话事件无限增长拖垮重渲染
          const MAX_EVENTS = 3000
          const events = evs.length ? [...prev.events, ...evs] : prev.events
          const trimmed = events.length > MAX_EVENTS ? events.slice(-MAX_EVENTS) : events
          let progress = prev.progress
          if (progress && progress.total > 0 && evs.length) progress = { done: events.length, total: progress.total }
          return { ...prev, events: trimmed, metrics: f.metrics || prev.metrics, progress }
        })
        break
      }
      case 'event':
        setView(prev => {
          const MAX_EVENTS = 3000
          const events = [...prev.events, f.event]
          const trimmed = events.length > MAX_EVENTS ? events.slice(-MAX_EVENTS) : events
          let progress = prev.progress
          if (progress && progress.total > 0) progress = { done: events.length, total: progress.total }
          return { ...prev, events: trimmed, progress }
        })
        break
      case 'metrics':
        setView(prev => ({ ...prev, metrics: f.metrics }))
        break
      case 'warning':
        setView(prev => ({ ...prev, warnings: [...prev.warnings, f.message] }))
        break
      case 'run/end':
        setView(prev => ({
          ...prev, result: f.result, status: f.result?.timeout ? '超时' : '完成',
          statusCls: f.result?.timeout || f.result?.finish_reason === 'error' ? 'err' : 'ok',
          metrics: f.result?.metrics || prev.metrics,
          progress: prev.progress ? { done: prev.progress.total, total: prev.progress.total } : prev.progress,
        }))
        refresh()
        break
      case 'run/cancel':
        setView(prev => ({ ...prev, status: '已取消', statusCls: 'err' }))
        break
      case 'error':
        setView(prev => ({ ...prev, status: '出错', statusCls: 'err', error: f.message }))
        break
      default:
    }
  }

  async function onSelect(s, tab = 'report') {
    setCur(s); setTab(tab)
    setView({ ...initialView(), _sid: s.session_id, status: '挂接中…', statusCls: 'run' })
    setView(prev => ({ ...prev, rawLoading: true }))
    getRaw(s.session_id).then(text => setView(prev => ({ ...prev, raw: text, rawLoading: false })))
    const j = await attach(s)
    if (!j.ok) setView(prev => ({ ...prev, status: '失败', statusCls: 'err', error: j.error }))
    else {
      // attach 成功：按模式设定状态（SSE run/start 帧可能先到，幂等覆盖）
      setView(prev => ({
        ...prev,
        status: j.mode === 'replay' ? '重放中' : '运行中',
        statusCls: 'run',
        progress: j.mode === 'replay' ? { done: 0, total: 0 } : null,
      }))
    }
    refresh()
  }

  function openSessionFromStats(sessionId) {
    const s = batchSessions.find(x => x.session_id === sessionId) || sessions.find(x => x.session_id === sessionId)
    if (!s) { alert('会话未找到: ' + sessionId); return }
    setViewMode('batch')
    onSelect(s, 'trajectory')
  }

  async function onAddPath(path) {
    const j = await addSessionPath(path)
    if (!j.ok) return alert('添加失败: ' + (j.error || ''))
    refresh()
  }

  const close = () => { if (cur) { detach(cur.session_id); setCur(null); setView(initialView()) } }

  function doExport() {
    if (!view._sid) return
    const html = buildReportHtml(view)
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `eval_${view._sid.slice(0, 24)}.html`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  const running = view.status === '挂接中…' || view.status === '运行中' || view.status === '重放中'
  // spinner 仅在「运行中 + 最近 2.5s 有数据帧」时转：避免 idle 时一直空转
  const spinning = running && (now - lastFrameAt) < 2500

  return (
    <div className="layout">
      <SessionList sessions={viewMode === 'batch' ? batchSessions : sessions} agentStatus={agentStatus} currentSid={view._sid}
                   onSelect={onSelect} onAddPath={onAddPath} onRefresh={refresh} />
      <main className="view">
        <header className="topbar">
          <h1>evalkit 评测看板</h1>
          <span className="agent-strip" title="agent 评测通道连接状态（每 15s 刷新）">
            {['claude', 'codemaker', 'dsh'].map(a => {
              const st = agentStatus[a]
              const state = st?.state || 'unknown'
              const cls = state === 'online' ? 'ok' : state === 'idle' ? 'idle' : state === 'offline' ? 'off' : ''
              const label = state === 'online' ? '在线' : state === 'idle' ? '空闲' : state === 'offline' ? '离线' : '未知'
              return (
                <span key={a} className={`astat ${cls}`} title={st?.reason || ''}>
                  <span className="adot" />{a}<em>{label}</em>
                </span>
              )
            })}
          </span>
          <span className={`pill ${conn === '已连接' ? 'ok' : 'err'}`}>{conn}</span>
          <span className="mode-switch">
            <button className={`mode ${viewMode === 'session' ? 'active' : ''}`} onClick={() => setViewMode('session')}>会话评测</button>
            <button className={`mode ${viewMode === 'batch' ? 'active' : ''}`} onClick={() => setViewMode('batch')}>批量评测</button>
            <button className={`mode ${viewMode === 'matrix' ? 'active' : ''}`} onClick={() => setViewMode('matrix')}>评测矩阵</button>
            <button className={`mode ${viewMode === 'stats' ? 'active' : ''}`} onClick={() => setViewMode('stats')}>统计总览</button>
            <button className={`mode ${viewMode === 'bank' ? 'active' : ''}`} onClick={() => setViewMode('bank')}>题库</button>
          </span>
          {cur && (
            <span className="cur mono">
              {cur.agent} · {cur.session_id}
              <button className="ghost" onClick={doExport} title="导出完整报告（HTML）">导出</button>
              <button className="ghost" onClick={close} title="关闭">✕</button>
            </span>
          )}
        </header>

        {viewMode === 'stats' ? (
          <StatsPanel onOpenSession={openSessionFromStats} />
        ) : viewMode === 'bank' ? (
          <TaskBank />
        ) : viewMode === 'matrix' ? (
          <EvalMatrix />
        ) : (
          <>
            {viewMode === 'batch' && !cur && <BatchEval />}
            {!cur ? (
              <div className="emptyview">{viewMode === 'batch'
                ? '从左侧选择批量评测会话查看单次报告'
                : '从左侧选择一个会话，查看评测报告与轨迹'}</div>
            ) : (
          <div className="wrap">
            {/* 顶部：精简 hero + 高密度统计条 */}
            <div className="hero">
              <div className="herohead">
                <h1>{viewMode === 'batch' ? '批量评测 · 单会话报告' : '单会话评测报告'}</h1>
                <span className={`hstate ${view.statusCls}`}>{view.status}</span>
                {spinning && <span className="spinner" />}
                <span className="sub mono">{view._sid}</span>
              </div>
              <StatStrip m={view.metrics || {}} />
              {view.progress && view.progress.total > 0 && (
                <div className="hprogress">
                  <div className="hbar"><div className="hfill" style={{ width: pct(view.progress) }} /></div>
                  <div className="hpct">{view.progress.done.toLocaleString()} / {view.progress.total.toLocaleString()} · {pct(view.progress)}</div>
                </div>
              )}
            </div>

            {/* 子 tab */}
            <div className="tabs">
              {[['report', '报告'], ['tasks', '任务'], ['trajectory', '轨迹'], ['raw', '原始日志']].map(([k, label]) => (
                <button key={k} className={`tab ${tab === k ? 'active' : ''}`} onClick={() => setTab(k)}>{label}</button>
              ))}
            </div>

            <div className="tabbody">
              {tab === 'report' && (view.result || view.metrics) && (
                <Report metrics={view.metrics || {}} result={view.result} />
              )}
              {tab === 'tasks' && <TasksPanel metrics={view.metrics || {}} onInspect={setInspector} />}
              {tab === 'trajectory' && <TrajectoryView events={view.events} onInspect={setInspector} />}
              {tab === 'raw' && (
                view.rawLoading ? <div className="muted" style={{ padding: 16 }}>加载中…</div>
                  : view.raw ? <RawLog text={view.raw} />
                    : <div className="muted empty" style={{ padding: 16 }}>无可浏览的原始日志</div>
              )}
            </div>

            {view.warnings.length > 0 && (
              <div className="warnbox">{view.warnings.map((w, i) => <div key={i} className="warn">warning: {w}</div>)}</div>
            )}
            {view.result && (
              <div className="final">
                {[
                  `session: ${view.result.session_id}`,
                  `finish_reason: ${view.result.finish_reason}`,
                  `cost ¥${view.metrics?.cost_cny ?? '—'}（估 ~¥${view.metrics?.cost_est_cny ?? '—'}） · duration ${view.metrics?.duration_ms_official ?? '—'}ms`,
                  view.result.log_path ? `log: ${view.result.log_path}` : '',
                  view.result.warnings?.length ? `warnings: ${view.result.warnings.join('; ')}` : '',
                ].filter(Boolean).join('\n')}
              </div>
            )}
          </div>
            )}
          </>
        )}
      </main>
      {/* 全局详情面板（右侧圆角悬浮，轨迹/任务工具链触发） */}
      {inspector && <Inspector record={inspector} onClose={() => setInspector(null)} />}
    </div>
  )
}

function pct(p) {
  if (!p || p.total <= 0) return '0%'
  return Math.min(100, Math.round(p.done / p.total * 100)) + '%'
}
