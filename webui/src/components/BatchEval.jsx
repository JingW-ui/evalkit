import React, { useEffect, useState } from 'react'
import { getTasks, saveTask, deleteTask, generateTasks, startBatch, stopBatch, getBatchStatus, fetchDkDevices } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const SKILLS = ['uu_remote', 'g66', 'airgattai', 'generic']
const LEVELS = ['L1', 'L2', 'L3', 'L4']
const COND_TYPES = ['evidence_anchor', 'negative_honesty', 'file_exists']
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', L4: '#cf222e' }

function condSummary(cond) {
  if (!cond || !cond.type) return '—'
  if (cond.type === 'evidence_anchor') return `锚点×${(cond.anchors || []).length}(≥${cond.threshold ?? 1})`
  if (cond.type === 'negative_honesty') return '诚实度'
  if (cond.type === 'file_exists') return `文件:${cond.path || ''}`
  return cond.type
}

// 批量评测：定义/生成任务 + 发起评测（agent 二选一 + skill + 重复次数 + 验收标准）
export default function BatchEval() {
  const [tasks, setTasks] = useState([])
  const [agent, setAgent] = useState('claude')
  const [skill, setSkill] = useState('uu_remote')
  const [repeat, setRepeat] = useState(2)
  const [perm, setPerm] = useState('bypassPermissions')
  const [batchState, setBatchState] = useState({})
  const [editing, setEditing] = useState(null)
  const [genDomain, setGenDomain] = useState('uu_remote')
  const [dkToken, setDkToken] = useState('')
  const [dkGroup, setDkGroup] = useState('12')
  const [dkDevices, setDkDevices] = useState([])
  const [device, setDevice] = useState('')
  const [info, setInfo] = useState('')

  useEffect(() => { refresh(); refreshStatus() }, [])
  useEffect(() => { const t = setInterval(refreshStatus, 3000); return () => clearInterval(t) }, [])
  function refresh() { getTasks().then(setTasks).catch(() => {}) }
  function refreshStatus() { getBatchStatus().then(setBatchState).catch(() => {}) }

  async function doStart() {
    setInfo('发起中…')
    const j = await startBatch({ agent, skill, repeat, permission_mode: perm, device: device || undefined })
    setInfo(j.ok ? `已发起：${j.total} 个任务 × ${j.repeat} 次（agent=${j.agent}）` : (j.error || '发起失败'))
    refreshStatus()
  }
  async function doStop() { await stopBatch(); refreshStatus() }
  async function doGen() {
    const j = await generateTasks({ domain: genDomain, params: device ? { device } : undefined })
    setInfo(j.ok ? `已生成 ${j.generated} 个任务（${genDomain} L1-L4${device ? ' · ' + device : ''}）` : (j.error || '生成失败'))
    refresh()
  }
  async function doSave() {
    const j = await saveTask(editing)
    if (j.ok) { setEditing(null); refresh(); setInfo('任务已保存') }
    else setInfo('保存失败: ' + (j.error || ''))
  }
  async function doDelete(taskId) { await deleteTask(taskId); refresh() }
  async function doFetchDk() {
    setInfo('拉取设备中…')
    const j = await fetchDkDevices({ token: dkToken || undefined, group_id: dkGroup })
    if (j.ok) { setDkDevices(j.devices || []); setInfo(`获取到 ${(j.devices || []).length} 台设备`) }
    else setInfo(j.error || '拉取失败')
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
      </div>
      <div className="launcher-row">
        <label>Skill</label>
        <select value={skill} onChange={e => setSkill(e.target.value)}>
          {SKILLS.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <label style={{ width: 'auto' }}>重复 n</label>
        <input type="number" value={repeat} onChange={e => setRepeat(parseInt(e.target.value || '1'))} style={{ width: 56 }} />
        <label style={{ width: 'auto' }}>权限</label>
        <select value={perm} onChange={e => setPerm(e.target.value)}>
          <option value="bypassPermissions">bypassPermissions</option>
          <option value="acceptEdits">acceptEdits</option>
          <option value="">默认</option>
        </select>
      </div>
      <div className="launcher-actions">
        <button className="primary" onClick={doStart} disabled={running || !tasks.length}>
          {running ? '评测中…' : '发起评测'}
        </button>
        <button className="ghost" onClick={doStop} disabled={!running}>停止</button>
        <span className="muted" style={{ fontSize: 11 }}>
          {running ? `运行中：${batchState.total} 任务 × ${batchState.repeat} 次` : (batchState.state === 'done' ? '上次评测已完成' : '')}
        </span>
      </div>
      {info && <div className="info">{info}</div>}

      <div className="launcher-row" style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
        <label>DK 配置</label>
        <input type="password" value={dkToken} onChange={e => setDkToken(e.target.value)} placeholder="dk_token（留空自动读 config）" style={{ flex: 1 }} />
        <label style={{ width: 'auto' }}>group</label>
        <input value={dkGroup} onChange={e => setDkGroup(e.target.value)} style={{ width: 56 }} />
        <button className="ghost" onClick={doFetchDk}>获取设备</button>
      </div>
      {dkDevices.length > 0 && (
        <div className="launcher-row">
          <label>设备标签</label>
          <select value={device} onChange={e => setDevice(e.target.value)}>
            <option value="">（不指定）</option>
            {dkDevices.map(d => (
              <option key={d.serialno} value={d.label}>{d.label}</option>
            ))}
          </select>
          <span className="muted" style={{ fontSize: 10 }}>{dkDevices.length} 台 · label 填充 {'{device}'}</span>
        </div>
      )}

      <div className="launcher-row" style={{ marginTop: 12, paddingTop: 10, borderTop: '1px solid var(--border)' }}>
        <label>生成任务</label>
        <select value={genDomain} onChange={e => setGenDomain(e.target.value)}>
          {SKILLS.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <button className="ghost" onClick={doGen}>一键生成 L1-L4</button>
        <button className="ghost" onClick={() => setEditing({
          task_id: '', level: 'L1', skill_expected: genDomain, query: '', repeat: null,
          success_condition: { type: 'evidence_anchor', anchors: [], threshold: 1 }, note: '',
        })}>新建任务</button>
      </div>

      <table style={{ marginTop: 10 }}>
        <thead><tr><th>task_id</th><th>L</th><th>skill</th><th>query</th><th>验收</th><th className="num">n</th><th></th></tr></thead>
        <tbody>
          {tasks.map(t => (
            <tr key={t.task_id}>
              <td className="mono" style={{ fontSize: 11 }}>{t.task_id}</td>
              <td><span className="badge" style={{ background: (LEVEL_COLOR[t.level] || '#6e7681') + '33', color: LEVEL_COLOR[t.level] || '#6e7681' }}>{t.level}</span></td>
              <td>{esc(t.skill_expected || '—')}</td>
              <td className="tq" title={t.query}>{esc((t.query || '').slice(0, 40))}</td>
              <td className="muted" style={{ fontSize: 10 }}>{condSummary(t.success_condition)}</td>
              <td className="num">{t.repeat ?? '—'}</td>
              <td style={{ whiteSpace: 'nowrap' }}>
                <button className="ghost" onClick={() => setEditing({ ...t })}>编辑</button>
                <button className="ghost" onClick={() => doDelete(t.task_id)}>删</button>
              </td>
            </tr>
          ))}
          {!tasks.length && <tr><td className="empty" colSpan="7" style={{ padding: 12 }}>暂无任务——先「生成」或「新建」</td></tr>}
        </tbody>
      </table>

      {editing && <TaskEditor task={editing} setTask={setEditing} onSave={doSave} onCancel={() => setEditing(null)} />}
    </div>
  )
}

function TaskEditor({ task, setTask, onSave, onCancel }) {
  const cond = task.success_condition || { type: 'evidence_anchor' }
  function setCond(patch) { setTask({ ...task, success_condition: { ...cond, ...patch } }) }
  return (
    <div className="modal">
      <div className="modal-body">
        <h3>编辑任务</h3>
        <div className="launcher-row"><label>task_id</label>
          <input value={task.task_id || ''} onChange={e => setTask({ ...task, task_id: e.target.value })} placeholder="如 uu_remote_L1_001" /></div>
        <div className="launcher-row">
          <label>级别</label>
          <select value={task.level} onChange={e => setTask({ ...task, level: e.target.value })}>
            {LEVELS.map(l => <option key={l} value={l}>{l}</option>)}
          </select>
          <label style={{ width: 'auto' }}>skill</label>
          <input value={task.skill_expected || ''} onChange={e => setTask({ ...task, skill_expected: e.target.value })} style={{ width: 120 }} />
          <label style={{ width: 'auto' }}>重复 n</label>
          <input type="number" value={task.repeat ?? ''} onChange={e => setTask({ ...task, repeat: e.target.value ? parseInt(e.target.value) : null })} style={{ width: 56 }} />
        </div>
        <div className="launcher-row"><label>query</label>
          <textarea value={task.query || ''} onChange={e => setTask({ ...task, query: e.target.value })} rows={2} style={{ flex: 1 }} /></div>

        <div className="launcher-row" style={{ marginTop: 6 }}>
          <label>验收类型</label>
          <select value={cond.type} onChange={e => setCond({ type: e.target.value })}>
            {COND_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        {cond.type === 'evidence_anchor' && (
          <>
            <div className="launcher-row"><label>anchors</label>
              <input value={(cond.anchors || []).join(', ')} onChange={e => setCond({ anchors: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="逗号分隔，如 验证码, DK" /></div>
            <div className="launcher-row"><label>threshold</label>
              <input type="number" value={cond.threshold ?? 1} onChange={e => setCond({ threshold: parseInt(e.target.value || '1') })} style={{ width: 60 }} /></div>
          </>
        )}
        {cond.type === 'negative_honesty' && (
          <>
            <div className="launcher-row"><label>negation</label>
              <input value={(cond.negation_markers || []).join(', ')} onChange={e => setCond({ negation_markers: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="失败/不存在/无法" /></div>
            <div className="launcher-row"><label>fake</label>
              <input value={(cond.fake_success_markers || []).join(', ')} onChange={e => setCond({ fake_success_markers: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="已写入/成功" /></div>
          </>
        )}
        {cond.type === 'file_exists' && (
          <>
            <div className="launcher-row"><label>path</label>
              <input value={cond.path || ''} onChange={e => setCond({ path: e.target.value })} placeholder="相对路径" /></div>
            <div className="launcher-row"><label>must_contain</label>
              <input value={(cond.must_contain || []).join(', ')} onChange={e => setCond({ must_contain: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })} placeholder="逗号分隔" /></div>
          </>
        )}

        <div className="launcher-row"><label>note</label>
          <input value={task.note || ''} onChange={e => setTask({ ...task, note: e.target.value })} /></div>
        <div className="launcher-actions">
          <button className="primary" onClick={onSave}>保存</button>
          <button className="ghost" onClick={onCancel}>取消</button>
        </div>
      </div>
    </div>
  )
}
