import React, { useEffect, useState } from 'react'
import { getProviders, launchTerminal, stopTerminal, listTerminals, listFs } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const fmtT = ms => ms ? new Date(ms).toLocaleTimeString() : '—'
const ROOTS = [
  ['工作目录', 'D:\\wy_projects\\work_4_log'],
  ['evalkit', 'D:\\wy_projects\\evalkit'],
  ['Claude 项目', 'C:\\Users\\wangjing71\\.claude\\projects'],
  ['D盘', 'D:\\'],
  ['C盘', 'C:\\'],
]

// 「拉起会话」独立页：选会话目录 + provider → 打开 claude 对话终端（新窗口）
export default function BatchLauncher({ onStarted, current }) {
  const [providers, setProviders] = useState([])
  const [provider, setProvider] = useState('default')
  const [cwd, setCwd] = useState('D:\\wy_projects\\work_4_log')
  const [fs, setFs] = useState(null); const [fsErr, setFsErr] = useState('')
  const [pickOpen, setPickOpen] = useState(false)
  const [terminals, setTerminals] = useState([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [info, setInfo] = useState('')

  useEffect(() => {
    getProviders().then(p => {
      setProviders(p)
      if (p.length && !p.some(x => x.name === 'default')) setProvider(p[0]?.name || '')
    }).catch(() => {})
    refreshTerminals()
  }, [])

  const refreshTerminals = () => listTerminals().then(setTerminals).catch(() => {})
  useEffect(() => {
    const t = setInterval(refreshTerminals, 5000)
    return () => clearInterval(t)
  }, [])

  const sel = providers.find(p => p.name === provider)
  const envPreview = sel?.env ? Object.entries(sel.env).map(([k, v]) => `${k}=${v}`).join('\n') : ''
  const hooksPreview = sel?.hooks ? Object.keys(sel.hooks).join(', ') : ''

  async function nav(path) {
    setFsErr(''); setFs(null)
    const j = await listFs(path)
    if (!j.ok) { setFsErr(j.error || '无法访问'); return }
    setCwd(j.path); setFs(j)
  }

  async function pickDir() {
    setPickOpen(false)
    setBusy(true); setErr(''); setInfo('')
    const j = await launchTerminal({ cwd, provider: provider === 'default' ? undefined : provider })
    setBusy(false)
    if (!j.ok) { setErr(j.error || '启动失败'); return }
    setInfo(`已在「${j.cwd}」打开 claude 对话终端（pid ${j.pid}）· ${j.provider}`)
    refreshTerminals()
    if (onStarted) onStarted(j)
  }

  async function stop(pid) {
    const j = await stopTerminal(pid)
    setInfo(j.message || `已停止 ${pid}`)
    refreshTerminals()
  }

  return (
    <div className="launcher panel">
      <h2>拉起 agent 会话窗口（claude 对话终端 · 按模型提供商）</h2>
      <div className="launcher-row">
        <label>会话目录</label>
        <input value={cwd} onChange={e => setCwd(e.target.value)} spellCheck={false}
               placeholder="claude 工作目录（会话日志将写到该目录项目）" />
        <button className="ghost" onClick={() => { setPickOpen(!pickOpen); if (!fs && !pickOpen) nav(cwd) }}>浏览</button>
      </div>
      {pickOpen && (
        <div className="fs-picker" style={{ margin: '4px 0 10px 80px' }}>
          <div className="fs-row">
            <input value={cwd} onChange={e => setCwd(e.target.value)}
                   onKeyDown={e => e.key === 'Enter' && nav(cwd)} spellCheck={false} />
            <button className="ghost" onClick={() => nav(cwd)}>跳转</button>
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
                <div className="fs-list fs-dirs" style={{ maxHeight: 180 }}>
                  {fs.parent != null && <div className="fs-item dir" onClick={() => nav(fs.parent)}>..</div>}
                  {fs.dirs.map(d => (
                    <div className="fs-item dir" key={d} onClick={() => nav(cwd.replace(/[\\/]$/, '') + '\\' + d)}>
                      ▸ {d}
                    </div>
                  ))}
                  {fs.dirs.length === 0 && fs.parent == null && <div className="muted empty">无子目录</div>}
                </div>
              </>
            )}
          </div>
          <div className="fs-row" style={{ marginTop: 6 }}>
            <button className="primary" onClick={pickDir} disabled={busy}>打开终端（此目录）</button>
            <button className="ghost" onClick={() => setPickOpen(false)}>关闭</button>
          </div>
        </div>
      )}
      <div className="launcher-row">
        <label>Provider</label>
        <select value={provider} onChange={e => setProvider(e.target.value)}>
          {providers.map(p => (
            <option key={p.name} value={p.name}>
              {p.name}{p.env?.ANTHROPIC_MODEL ? ` · ${p.env.ANTHROPIC_MODEL}` : ''}
            </option>
          ))}
        </select>
        <span className="launcher-preview mono" title={`env 覆盖\n${envPreview}`}>
          {sel?.env?.ANTHROPIC_BASE_URL || '(继承系统配置)'}
          {hooksPreview ? ` · hooks: ${hooksPreview}` : ''}
        </span>
      </div>
      {envPreview && <div className="launcher-env mono">{esc(envPreview)}</div>}
      {err && <div className="warn">{err}</div>}
      {info && <div className="info">{info}</div>}
      <div className="launcher-actions">
        <button className="primary" onClick={pickDir} disabled={busy}>
          {busy ? '打开中…' : '打开 claude 终端'}
        </button>
        <span className="muted" style={{ fontSize: 10 }}>
          不填 query：打开的是 agent 本身的交互式对话窗口（新控制台），在窗口里直接对话；日志自动接入评测
        </span>
      </div>

      {terminals.length > 0 && (
        <div className="terminals" style={{ marginTop: 14 }}>
          <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>已打开终端</div>
          {terminals.map(t => (
            <div key={t.pid} className="term-item">
              <span className="mono" style={{ color: 'var(--green)' }}>●</span>
              <span className="mono" style={{ fontSize: 11 }}>pid {t.pid}</span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--ink2)', flex: 1 }}>{t.cwd}</span>
              <span className="muted" style={{ fontSize: 10 }}>{t.provider || 'default'} · {fmtT(t.started_at)}</span>
              <button className="ghost" onClick={() => stop(t.pid)} title="结束终端">✕</button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
