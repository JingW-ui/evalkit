import React, { useState, useEffect } from 'react'
import { listFs, addSessionPath, renameSession, removeSession } from '../api.js'

const fmtT = ms => ms ? new Date(ms).toLocaleString() : '—'
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtSize = n => n == null ? '' : n >= 1048576 ? (n/1048576).toFixed(1)+'MB' : n >= 1024 ? (n/1024).toFixed(0)+'KB' : n+'B'
const AGENT_LABEL = { claude: 'claude', dsh: 'dsh', airlab: 'airlab', codemaker: 'codemaker', eval: 'eval' }
const ROOTS = [
  ['会话样例', 'D:\\wy_projects\\evalkit\\sessions'],
  ['Claude 项目', 'C:\\Users\\wangjing71\\.claude\\projects'],
  ['Codemaker 会话库', 'C:\\Users\\wangjing71\\.local\\share\\codemaker'],
  ['D盘', 'D:\\'],
  ['C盘', 'C:\\'],
]

export default function SessionList({ sessions, agentStatus = {}, currentSid, onSelect, onAddPath, onRefresh }) {
  const [addOpen, setAddOpen] = useState(false)
  const [curPath, setCurPath] = useState('D:\\wy_projects\\evalkit\\sessions')
  const [fs, setFs] = useState(null)
  const [fsErr, setFsErr] = useState('')
  const [manual, setManual] = useState('')
  const [busy, setBusy] = useState(false)
  const [renaming, setRenaming] = useState(null)   // 正在重命名的 session_id
  const [renameVal, setRenameVal] = useState('')
  const [expanded, setExpanded] = useState({})     // agent 分组默认收起（undefined=收起）

  // 按 agent 类别分组（组内运行中在前）
  const byAgent = {}
  for (const s of sessions) (byAgent[s.agent] = byAgent[s.agent] || []).push(s)
  const agentGroups = Object.entries(byAgent).sort((a, b) => {
    const rank = a[0] === 'eval' ? 3 : a[0] === 'airlab' ? 2 : a[0] === 'dsh' ? 1 : 0
    const rank2 = b[0] === 'eval' ? 3 : b[0] === 'airlab' ? 2 : b[0] === 'dsh' ? 1 : 0
    return rank - rank2
  })
  for (const [, items] of agentGroups) {
    items.sort((a, b) => (a.state === 'live' ? 0 : 1) - (b.state === 'live' ? 0 : 1) || ((b.updated_at || 0) - (a.updated_at || 0)))
  }

  // 选中会话所在分组自动展开（刷新后保持可见）
  useEffect(() => {
    if (!currentSid) return
    setExpanded(prev => {
      const next = { ...prev }
      for (const [agent, items] of agentGroups) {
        if (items.some(s => s.session_id === currentSid) && !next[agent]) {
          next[agent] = true
        }
      }
      return next
    })
  }, [currentSid])  // eslint-disable-line react-hooks/exhaustive-deps

  function toggleGroup(agent) {
    setExpanded(prev => ({ ...prev, [agent]: !prev[agent] }))
  }

  function expandAll() {
    const all = {}
    for (const [agent] of agentGroups) all[agent] = true
    setExpanded(all)
  }

  function collapseAll() {
    setExpanded({})
  }

  function openPicker(path) { setAddOpen(true); nav(path) }

  async function nav(path) {
    setFsErr(''); setFs(null)
    const j = await listFs(path)
    if (!j.ok) { setFsErr(j.error || '无法访问'); return }
    setCurPath(j.path)
    setFs(j)
  }

  async function pickFile(f) {
    const full = curPath.replace(/[\\/]$/, '') + '\\' + f.name
    setBusy(true)
    const j = await addSessionPath(full)
    setBusy(false)
    if (!j.ok) { alert('添加失败: ' + (j.error || '')); return }
    onAddPath(full); setAddOpen(false)
  }

  async function pickDir() {
    setBusy(true)
    const j = await addSessionPath(curPath)
    setBusy(false)
    if (!j.ok) { alert('添加失败: ' + (j.error || '')); return }
    alert(`已添加 ${j.count || 1} 个会话`)
    onAddPath(curPath); setAddOpen(false)
  }

  async function submitManual() {
    if (!manual.trim()) return
    setBusy(true)
    const j = await addSessionPath(manual.trim())
    setBusy(false)
    if (!j.ok) { alert('添加失败: ' + (j.error || '')); return }
    onAddPath(manual.trim()); setManual(''); setAddOpen(false)
  }

  function startRename(s) {
    setRenaming(s.session_id)
    setRenameVal(s.extra?.display_name || s.session_id)
  }
  async function submitRename(sid) {
    await renameSession(sid, renameVal)
    setRenaming(null)
    onRefresh()
  }
  async function doRemove(s) {
    if (!window.confirm(`从列表移除会话？\n${s.extra?.display_name || s.session_id}\n（不删除文件）`)) return
    await removeSession(s.session_id)
    onRefresh()
  }

  return (
    <aside className="side">
      <div className="sidehead">
        <span className="t">会话列表 · 选中即评测</span>
        <span style={{ display: 'flex', gap: 6 }}>
          <button className="ghost" onClick={onRefresh} title="刷新">↻</button>
          <button className="ghost" onClick={() => openPicker('D:\\wy_projects\\evalkit\\sessions')}
                  title="选择文件或目录添加会话">＋</button>
        </span>
      </div>

      {addOpen && (
        <div className="fs-picker">
          <div className="fs-row">
            <input value={curPath} onChange={e => setCurPath(e.target.value)}
                   onKeyDown={e => e.key === 'Enter' && nav(curPath)} spellCheck={false} />
            <button className="ghost" onClick={() => nav(curPath)}>跳转</button>
            <button className="primary" onClick={pickDir} disabled={busy} title="添加当前目录下所有会话">选此目录</button>
          </div>
          <div className="fs-roots">
            {ROOTS.map(([label, p]) => (
              <button key={p} className="ghost" onClick={() => nav(p)}>{label}</button>
            ))}
          </div>
          {fsErr && <div className="warn">{fsErr}</div>}
          <div className="fs-body">
            {fs && (
              <>
                <div className="fs-list fs-dirs">
                  {fs.parent != null && <div className="fs-item dir" onClick={() => nav(fs.parent)}>..</div>}
                  {fs.dirs.map(d => (
                    <div className="fs-item dir" key={d} onClick={() => nav(curPath.replace(/[\\/]$/, '') + '\\' + d)}>
                      📁 {d}
                    </div>
                  ))}
                  {fs.dirs.length === 0 && fs.parent == null && <div className="muted empty">无子目录</div>}
                </div>
                <div className="fs-list fs-files">
                  {fs.files.map(f => (
                    <div className={`fs-item file ${f.kind ? 'kind' : ''}`} key={f.name}
                         onClick={() => f.kind && pickFile(f)} title={f.kind ? `添加会话（${f.kind}）` : '非会话文件'}>
                      <span className={`ab ${f.kind || 'na'}`}>{f.kind || '—'}</span>
                      <span className="fname">{f.name}</span>
                      <span className="fsize">{fmtSize(f.size)}</span>
                    </div>
                  ))}
                  {fs.files.length === 0 && <div className="muted empty">无会话文件</div>}
                </div>
              </>
            )}
            {busy && <div className="muted" style={{ padding: 6 }}>处理中…</div>}
          </div>
          <div className="fs-row fs-manual">
            <input placeholder="或直接粘贴会话文件路径" value={manual} onChange={e => setManual(e.target.value)}
                   onKeyDown={e => e.key === 'Enter' && submitManual()} spellCheck={false} />
            <button className="ghost" onClick={submitManual} disabled={busy}>添加</button>
            <button className="ghost" onClick={() => setAddOpen(false)}>关闭</button>
          </div>
        </div>
      )}

      <div className="sesslist">
        <div className="sgroup sgroup-actions">
          <span className="muted" style={{ fontSize: 9 }}>分组</span>
          <button className="ghost" onClick={expandAll} title="展开全部分组" style={{ fontSize: 10 }}>全部展开</button>
          <button className="ghost" onClick={collapseAll} title="收起全部分组" style={{ fontSize: 10 }}>全部收起</button>
        </div>
        {agentGroups.map(([agent, items]) => {
          const live = items.filter(s => s.state === 'live').length
          const isOpen = !!expanded[agent]
          const ast = agentStatus[agent]
          const astCls = ast?.state === 'online' ? 'ok' : ast?.state === 'idle' ? 'idle' : ast?.state === 'offline' ? 'off' : ''
          return (
            <React.Fragment key={agent}>
              <div className="sgroup" onClick={() => toggleGroup(agent)} title={isOpen ? '收起分组' : '展开分组'}>
                <span className={`sg-arrow ${isOpen ? 'open' : ''}`}>▸</span>
                <span className={`sg-dot ${astCls}`} title={ast?.reason || '未知连接状态'} />
                <span className="sg-agent">{AGENT_LABEL[agent] || agent}</span>
                <span className="sg-count">{items.length}</span>
                {live > 0 && <span className="sg-live">{live} 运行中</span>}
              </div>
              {isOpen && items.map(s => {
                const name = s.extra?.display_name || s.session_id
                return (
                  <div key={s.agent + ':' + s.session_id}
                       className={`sitem ${s.session_id === currentSid ? 'active' : ''}`}
                       onClick={() => onSelect(s)}>
                    <div className="row1">
                      <span className={`dot ${s.state}`} />
                      {renaming === s.session_id ? (
                        <input className="rename-input" value={renameVal} autoFocus
                               onClick={e => e.stopPropagation()}
                               onChange={e => setRenameVal(e.target.value)}
                               onKeyDown={e => { e.stopPropagation(); if (e.key === 'Enter') submitRename(s.session_id); if (e.key === 'Escape') setRenaming(null) }}
                               onBlur={() => submitRename(s.session_id)} />
                      ) : (
                        <span className="sid" title={s.session_id}>
                          {s.extra?.display_name ? name : s.session_id}
                        </span>
                      )}
                      <span className="sops">
                        <button className="ghost" title="重命名" onClick={e => { e.stopPropagation(); startRename(s) }}>✎</button>
                        <button className="ghost" title="移除" onClick={e => { e.stopPropagation(); doRemove(s) }}>✕</button>
                      </span>
                    </div>
                    {s.extra?.display_name && <div className="q mono">{s.session_id}</div>}
                    <div className="q">{esc(s.query || '')}</div>
                    <div className="tm">{fmtT(s.updated_at)} · {s.source}</div>
                  </div>
                )
              })}
            </React.Fragment>
          )
        })}
        {!sessions.length && <div className="muted empty" style={{ padding: 8 }}>未发现会话</div>}
      </div>
    </aside>
  )
}

