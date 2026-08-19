import React, { useEffect, useState } from 'react'
import { getTasks, startBatch, stopBatch, getBatchStatus, fetchDkDevices, getModels, launchTerminal, stopTerminal, listTerminals, listFs } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', 'L3-S': '#b083f0', L4: '#cf222e' }

// 批量评测：选任务（默认全选，可按 skill/级别筛选）→ 发起评测
export default function BatchEval() {
  const [tasks, setTasks] = useState([])           // 启用任务全集
  const [agent, setAgent] = useState('claude')
  const [repeat, setRepeat] = useState(2)
  const [perm, setPerm] = useState('bypassPermissions')
  const [batchState, setBatchState] = useState({})
  const [cwd, setCwd] = useState('D:\\wy_projects\\work_4_log')
  const [dkToken, setDkToken] = useState('')
  const [dkGroup, setDkGroup] = useState('12')
  const [dkDevices, setDkDevices] = useState([])
  const [device, setDevice] = useState('')
  const [models, setModels] = useState([])
  const [model, setModel] = useState('')
  const [terminals, setTerminals] = useState([])
  const [termOpen, setTermOpen] = useState(false)
  const [fs, setFs] = useState(null)
  const [fsErr, setFsErr] = useState('')
  const [fsPickOpen, setFsPickOpen] = useState(false)
  const [info, setInfo] = useState('')
  // 任务选择 + 筛选
  const [selected, setSelected] = useState(null)   // null = 全选（默认）
  const [fSkill, setFSkill] = useState('')
  const [fLevel, setFLevel] = useState('')

  useEffect(() => { refresh(); refreshStatus(); getModels().then(setModels).catch(() => {}); refreshTerminals() }, [])
  useEffect(() => { const t = setInterval(refreshStatus, 3000); const t2 = setInterval(refreshTerminals, 5000); return () => { clearInterval(t); clearInterval(t2) } }, [])

  function refresh() { getTasks().then(ts => setTasks((ts || []).filter(t => t.enabled !== 0))).catch(() => {}) }
  function refreshStatus() { getBatchStatus().then(setBatchState).catch(() => {}) }
  function refreshTerminals() { listTerminals().then(setTerminals).catch(() => {}) }

  const skills = [...new Set(tasks.map(t => t.skill_expected).filter(Boolean))]
  const levels = [...new Set(tasks.map(t => t.level).filter(Boolean))]
  const filtered = tasks.filter(t =>
    (!fSkill || t.skill_expected === fSkill) && (!fLevel || t.level === fLevel))
  const allIds = tasks.map(t => t.task_id)
  const selectedIds = selected === null ? allIds : [...selected]
  const filteredIds = filtered.map(t => t.task_id)
  const allChecked = filteredIds.length > 0 && filteredIds.every(id => selectedIds.includes(id))

  function toggle(tid) {
    setSelected(prev => {
      const base = prev === null ? new Set(allIds) : new Set(prev)
      if (base.has(tid)) base.delete(tid); else base.add(tid)
      return base
    })
  }
  function toggleAll() {
    setSelected(prev => {
      const base = prev === null ? new Set(allIds) : new Set(prev)
      const allSel = filteredIds.every(id => base.has(id))
      const next = new Set(base)
      filteredIds.forEach(id => allSel ? next.delete(id) : next.add(id))
      return next
    })
  }

  async function doStart() {
    if (!selectedIds.length) { setInfo('请先勾选任务'); return }
    setInfo('发起中…')
    const j = await startBatch({
      agent, repeat, permission_mode: perm, device: device || undefined, cwd: cwd || undefined,
      model: model ? (agent === 'codemaker' ? `netease-codemaker/${model}` : model) : undefined,
      provider: (agent === 'claude' && model) ? 'codemaker_deepseek' : undefined,
      task_ids: selectedIds,
    })
    setInfo(j.ok ? `已发起：${j.total} 个任务 × ${j.repeat} 次（agent=${j.agent}）` : (j.error || '发起失败'))
    refreshStatus()
  }
  async function doStop() { await stopBatch(); refreshStatus() }

  async function doFetchDk() {
    setInfo('拉取设备中…')
    const j = await fetchDkDevices({ token: dkToken || undefined, group_id: dkGroup })
    if (j.ok) { setDkDevices(j.devices || []); setInfo(`获取到 ${(j.devices || []).length} 台设备`) }
    else setInfo(j.error || '拉取失败')
  }

  async function doLaunchTerminal() {
    setInfo('打开终端中…')
    const j = await launchTerminal({
      cwd, agent,
      model: model ? (agent === 'codemaker' ? `netease-codemaker/${model}` : model) : undefined,
      provider: (agent === 'claude' && model) ? 'codemaker_deepseek' : undefined,
    })
    setInfo(j.ok ? `已在「${j.cwd}」打开 ${agent} 终端（pid ${j.pid}）` : (j.error || '启动失败'))
    refreshTerminals()
  }
  async function doStopTerminal(pid) { await stopTerminal(pid); refreshTerminals() }
  async function navFs(path) {
    setFsErr(''); setFs(null)
    const j = await listFs(path)
    if (!j.ok) { setFsErr(j.error || '无法访问'); return }
    setCwd(j.path); setFs(j)
  }

  const running = batchState.state === 'running'

  return (
    <div className="panel">
      <h2>批量评测</h2>

      <div className="launcher-row">
        <label>Agent</label>
        <div className="agent-toggle">
          {['claude', 'codemaker'].map(a => (
            <button key={a} className={`ghost ${agent === a ? 'active' : ''}`} onClick={() => setAgent(a)}>{a}</button>
          ))}
        </div>
        <label style={{ width: 'auto' }}>模型</label>
        <select value={model} onChange={e => setModel(e.target.value)} style={{ width: 180, flex: 'none' }}>
          <option value="">（默认）</option>
          {models.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
        <label style={{ width: 'auto' }}>执行目录</label>
        <input value={cwd} onChange={e => setCwd(e.target.value)} spellCheck={false}
               placeholder="含 .mcp.json / .claude/skills" title="MCP/skill 从该目录加载" style={{ width: 300, flex: 'none' }} />
        <button className="ghost" onClick={() => { setFsPickOpen(!fsPickOpen); if (!fs && !fsPickOpen) navFs(cwd) }}>浏览</button>
      </div>
      {fsPickOpen && (
        <div className="fs-picker" style={{ margin: '4px 0 10px 80px' }}>
          <div className="fs-row">
            <input value={cwd} onChange={e => setCwd(e.target.value)} onKeyDown={e => e.key === 'Enter' && navFs(cwd)} spellCheck={false} />
            <button className="ghost" onClick={() => navFs(cwd)}>跳转</button>
          </div>
          <div className="fs-body">
            {fs && (
              <div className="fs-list fs-dirs" style={{ maxHeight: 180 }}>
                {fs.parent != null && <div className="fs-item dir" onClick={() => navFs(fs.parent)}>..</div>}
                {fs.dirs.map(d => (
                  <div className="fs-item dir" key={d} onClick={() => navFs(cwd.replace(/[\\/]$/, '') + '\\' + d)}>▸ {d}</div>
                ))}
                {fs.dirs.length === 0 && fs.parent == null && <div className="muted empty">无子目录</div>}
              </div>
            )}
          </div>
          {fsErr && <div className="warn">{fsErr}</div>}
        </div>
      )}

      <div className="launcher-row">
        <label style={{ width: 'auto' }}>重复 n</label>
        <input type="number" value={repeat} onChange={e => setRepeat(parseInt(e.target.value || '1'))} style={{ width: 56 }} />
        <label style={{ width: 'auto' }}>权限</label>
        <select value={perm} onChange={e => setPerm(e.target.value)}>
          <option value="bypassPermissions">bypassPermissions</option>
          <option value="acceptEdits">acceptEdits</option>
          <option value="">默认</option>
        </select>
        <label style={{ width: 'auto' }}>设备</label>
        <select value={device} onChange={e => setDevice(e.target.value)} style={{ flex: 1, minWidth: 180 }}>
          <option value="">（不指定设备）</option>
          {dkDevices.map(d => {
            const st = d.online ? (d.occupied ? `占用${d.occupy_username ? '·' + d.occupy_username : ''}` : '空闲') : '离线'
            return <option key={d.serialno} value={d.serialno}>{d.label} · {st}{d.ip ? ` · ${d.ip}` : ''}</option>
          })}
        </select>
        <input type="password" value={dkToken} onChange={e => setDkToken(e.target.value)} placeholder="DK token（留空自动读）" style={{ width: 150 }} />
        <input value={dkGroup} onChange={e => setDkGroup(e.target.value)} title="dk_group" style={{ width: 48, flex: 'none' }} />
        <button className="ghost" onClick={doFetchDk}>获取设备</button>
      </div>

      <div className="launcher-actions">
        <button className="primary" onClick={doStart} disabled={running || !selectedIds.length}>
          {running ? '评测中…' : `发起评测（${selectedIds.length} 题）`}
        </button>
        <button className="ghost" onClick={doStop} disabled={!running}>停止</button>
        <button className="ghost" onClick={doLaunchTerminal}>打开交互终端</button>
        <span className="muted" style={{ fontSize: 11 }}>
          {running ? `运行中：${batchState.total} 任务 × ${batchState.repeat} 次` : (batchState.state === 'done' ? '上次评测已完成' : '')}
        </span>
      </div>
      {info && <div className="info">{info}</div>}

      <div className="launcher-row" style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
        <label><input type="checkbox" checked={allChecked} onChange={toggleAll} /> 全选</label>
        <label style={{ width: 'auto' }}>skill</label>
        <select value={fSkill} onChange={e => setFSkill(e.target.value)}><option value="">全部</option>{skills.map(s => <option key={s} value={s}>{s}</option>)}</select>
        <label style={{ width: 'auto' }}>级别</label>
        <select value={fLevel} onChange={e => setFLevel(e.target.value)}><option value="">全部</option>{levels.map(l => <option key={l} value={l}>{l}</option>)}</select>
        <span className="muted" style={{ fontSize: 11 }}>已选 {selectedIds.length}/{tasks.length}</span>
      </div>

      <table style={{ marginTop: 6 }}>
        <colgroup>
          <col style={{ width: 28 }} /><col style={{ width: '22%' }} />
          <col style={{ width: 48 }} /><col style={{ width: '12%' }} /><col style={{ width: 'auto' }} />
        </colgroup>
        <thead><tr><th></th><th>task_id</th><th>L</th><th>skill</th><th>query</th></tr></thead>
        <tbody>
          {filtered.map(t => (
            <tr key={t.task_id}>
              <td><input type="checkbox" checked={selectedIds.includes(t.task_id)} onChange={() => toggle(t.task_id)} /></td>
              <td className="mono" style={{ fontSize: 11 }}>{esc(t.task_id)}</td>
              <td><span className="badge" style={{ background: (LEVEL_COLOR[t.level] || '#6e7681') + '33', color: LEVEL_COLOR[t.level] || '#6e7681' }}>{t.level || '?'}</span></td>
              <td>{esc(t.skill_expected || '—')}</td>
              <td className="tq" title={t.query}>{esc((t.query || '').slice(0, 60))}</td>
            </tr>
          ))}
          {!filtered.length && <tr><td className="empty" colSpan="5" style={{ padding: 12 }}>无任务——到「题库」页定义题目</td></tr>}
        </tbody>
      </table>

      <div className="terminals" style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
        <div className="muted" style={{ fontSize: 11, cursor: 'pointer' }} onClick={() => setTermOpen(!termOpen)}>
          {termOpen ? '▾' : '▸'} 已打开终端（{terminals.length}）
        </div>
        {termOpen && terminals.length > 0 && (
          <div style={{ marginTop: 4 }}>
            {terminals.map(t => (
              <div key={t.pid} className="term-item">
                <span className="mono" style={{ color: 'var(--green)' }}>●</span>
                <span className="mono" style={{ fontSize: 11 }}>pid {t.pid}</span>
                <span className="mono" style={{ fontSize: 11, color: 'var(--ink2)', flex: 1 }}>{t.cwd}</span>
                <span className="muted" style={{ fontSize: 10 }}>{t.agent || 'claude'} · {t.provider || 'default'}</span>
                <button className="ghost" onClick={() => doStopTerminal(t.pid)} title="结束终端">✕</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
