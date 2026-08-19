import React, { useEffect, useState } from 'react'
import { getTasks, saveTask, deleteTask } from '../api.js'

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))
const LEVELS = ['L1', 'L2', 'L3', 'L3-S', 'L4']
const SKILLS = ['base', 'g66', 'uu_remote']
const LEVEL_COLOR = { L1: '#58a6ff', L2: '#2da44e', L3: '#f0883e', 'L3-S': '#b083f0', L4: '#cf222e' }
const TYPE_LABEL = { skill: 'skill', mcp: 'mcp', local_script: '脚本' }

const emptyForm = () => ({
  task_id: '', title: '', level: 'L1', skill_expected: 'base',
  query: '', device_var: '{device}',
  result: '', process: '',
  tools: '',
  sr_threshold: 0.8, n_min: 20, veto: false,
  success_condition: '',
  prep: '', note: '', enabled: true,
})

export default function TaskBank() {
  const [tasks, setTasks] = useState(null)
  const [editing, setEditing] = useState(null)   // null=列表 / 'new' / task_id
  const [form, setForm] = useState(emptyForm())
  const [msg, setMsg] = useState('')

  useEffect(() => { refresh() }, [])
  async function refresh() { setTasks(await getTasks()) }

  function toForm(t) {
    const ea = t.expected_answer || {}
    const ac = t.accept_criteria || {}
    return {
      task_id: t.task_id || '', title: t.title || '', level: t.level || 'L1',
      skill_expected: t.skill_expected || 'base',
      query: t.query || '', device_var: t.device_var || '{device}',
      result: ea.result || '',
      process: (ea.process || []).join('\n'),
      tools: (t.tools_required || []).map(x => `${x.tool} ${x.type || 'mcp'}`).join('\n'),
      sr_threshold: ac.sr_threshold ?? 0.8,
      n_min: ac.n_min ?? 20,
      veto: !!ac.veto,
      success_condition: t.success_condition ? JSON.stringify(t.success_condition) : '',
      prep: t.prep || '', note: t.note || '', enabled: t.enabled !== 0,
    }
  }

  function startNew() { setForm(emptyForm()); setEditing('new'); setMsg('') }
  function startEdit(t) { setForm(toForm(t)); setEditing(t.task_id); setMsg('') }
  function cancel() { setEditing(null); setMsg('') }

  async function submit() {
    if (!form.task_id.trim()) { setMsg('task_id 必填'); return }
    const expected_answer = {
      result: form.result,
      process: form.process.split('\n').map(s => s.trim()).filter(Boolean),
    }
    const tools_required = form.tools.split('\n').map(s => s.trim()).filter(Boolean)
      .map(line => { const [tool, type] = line.split(/\s+/); return { tool, type: type || 'mcp', required: true } })
    const accept_criteria = {
      sr_threshold: Number(form.sr_threshold) || 0,
      n_min: Number(form.n_min) || 0,
      veto: !!form.veto,
    }
    let success_condition = {}
    if (form.success_condition.trim()) {
      try { success_condition = JSON.parse(form.success_condition) }
      catch { setMsg('success_condition JSON 解析失败'); return }
    }
    const task = {
      task_id: form.task_id.trim(), title: form.title, level: form.level,
      skill_expected: form.skill_expected, query: form.query,
      device_var: form.device_var,
      expected_answer, tools_required, accept_criteria, success_condition,
      prep: form.prep, note: form.note, enabled: form.enabled ? 1 : 0,
    }
    const j = await saveTask(task)
    if (j.ok) { setEditing(null); setMsg(j.note || '已保存'); refresh() }
    else setMsg(j.error || '保存失败')
  }

  async function del(t) {
    if (!window.confirm(`删除题目 ${t.task_id}？`)) return
    await deleteTask(t.task_id)
    refresh()
  }

  if (tasks === null) return <div className="muted" style={{ padding: 20 }}>加载中…</div>

  return (
    <div className="panel">
      <h2>题库 · {tasks.length} 题
        <button className="ghost" onClick={refresh} style={{ float: 'right', marginLeft: 6 }}>↻</button>
        {!editing && <button className="ghost" onClick={startNew} style={{ float: 'right' }}>+ 新增题目</button>}
      </h2>
      {msg && <div className="warnbox" style={{ padding: '6px 10px' }}>{msg}</div>}

      {editing ? (
        <TaskForm form={form} setForm={setForm} editing={editing} onSubmit={submit} onCancel={cancel} />
      ) : (
        <table style={{ tableLayout: 'fixed' }}>
          <colgroup>
            <col style={{ width: '16%' }} /><col style={{ width: '16%' }} />
            <col style={{ width: '6%' }} /><col style={{ width: '9%' }} />
            <col style={{ width: '24%' }} /><col style={{ width: '15%' }} />
            <col style={{ width: '8%' }} /><col style={{ width: '14%' }} />
          </colgroup>
          <thead><tr>
            <th>题目</th><th>task_id</th><th>L</th><th>skill</th><th>query</th><th>工具</th><th>状态</th><th>操作</th>
          </tr></thead>
          <tbody>
            {tasks.map(t => (
              <tr key={t.task_id}>
                <td title={t.title}>{esc(t.title || '—')}</td>
                <td className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.task_id}>{esc(t.task_id)}</td>
                <td><span className="badge" style={{ background: (LEVEL_COLOR[t.level] || '#6e7681') + '33', color: LEVEL_COLOR[t.level] || '#6e7681' }}>{t.level || '?'}</span></td>
                <td>{esc(t.skill_expected || '—')}</td>
                <td style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.query}>{esc(t.query || '—')}</td>
                <td>{(t.tools_required || []).map(x => (
                  <span key={x.tool} className="badge" style={{ marginRight: 3, background: '#6e7681' + '22', color: '#6e7681' }}>{esc(x.tool)}</span>
                ))}</td>
                <td><span style={{ color: t.enabled === 0 ? 'var(--red)' : 'var(--green)' }}>{t.enabled === 0 ? '停用' : '启用'}</span></td>
                <td>
                  <button className="ghost" onClick={() => startEdit(t)}>编辑</button>
                  <button className="ghost" onClick={() => del(t)}>删除</button>
                </td>
              </tr>
            ))}
            {!tasks.length && <tr><td className="empty" colSpan="8" style={{ padding: 12 }}>题库为空——重启服务自动从 papers/ 导入，或点「新增题目」</td></tr>}
          </tbody>
        </table>
      )}
      <p className="muted" style={{ fontSize: 11, marginTop: 8 }}>
        权威源 = papers/*.yaml（git 管理）；编辑保存只写回 YAML 并提示人工 commit，不自动提交。
      </p>
    </div>
  )
}

function TaskForm({ form, setForm, editing, onSubmit, onCancel }) {
  const set = (k, v) => setForm({ ...form, [k]: v })
  const showProcess = form.level === 'L3' || form.level === 'L3-S'
  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 14, marginBottom: 12 }}>
      <h3 style={{ margin: '0 0 10px' }}>{editing === 'new' ? '新增题目' : `编辑 ${editing}`}</h3>
      <div className="launcher-row" style={{ flexWrap: 'wrap' }}>
        <label>task_id <input value={form.task_id} disabled={editing !== 'new'} onChange={e => set('task_id', e.target.value)} placeholder="L3_g66_deploy" /></label>
        <label>题目名 <input value={form.title} onChange={e => set('title', e.target.value)} /></label>
        <label>级别 <select value={form.level} onChange={e => set('level', e.target.value)}>{LEVELS.map(l => <option key={l} value={l}>{l}</option>)}</select></label>
        <label>skill <select value={form.skill_expected} onChange={e => set('skill_expected', e.target.value)}>{SKILLS.map(s => <option key={s} value={s}>{s}</option>)}</select></label>
      </div>
      <div className="launcher-row" style={{ flexWrap: 'wrap' }}>
        <label style={{ flex: 1 }}>query <input value={form.query} onChange={e => set('query', e.target.value)} placeholder="部署组内 G66 资源到设备 {device}..." /></label>
        <label>设备变量 <input value={form.device_var} onChange={e => set('device_var', e.target.value)} /></label>
        <label>前置准备 <input value={form.prep} onChange={e => set('prep', e.target.value)} placeholder="人工锁屏（可选）" /></label>
      </div>
      <label style={{ display: 'block', margin: '8px 0' }}>预计答案 · 最终结果（result，主判据）
        <textarea rows={2} style={{ width: '100%' }} value={form.result} onChange={e => set('result', e.target.value)} placeholder="client.exe 进程在目标机运行..." />
      </label>
      {showProcess && (
        <label style={{ display: 'block', margin: '8px 0' }}>过程验收点（process，仅 L3/L3-S，每行一条）
          <textarea rows={3} style={{ width: '100%' }} value={form.process} onChange={e => set('process', e.target.value)} placeholder={'model→serialno 映射正确\n启动后用 tasklist 验证进程'} />
        </label>
      )}
      <label style={{ display: 'block', margin: '8px 0' }}>工具标注（tools_required，每行「工具名 类型」）
        <textarea rows={2} style={{ width: '100%' }} value={form.tools} onChange={e => set('tools', e.target.value)} placeholder={'g66 skill\nlist_devices mcp\noccupy_device mcp'} />
        <span className="muted" style={{ fontSize: 11 }}>类型：skill / mcp / local_script</span>
      </label>
      <div className="launcher-row" style={{ flexWrap: 'wrap', margin: '8px 0' }}>
        <label>sr_threshold <input type="number" step="0.1" style={{ width: 70 }} value={form.sr_threshold} onChange={e => set('sr_threshold', e.target.value)} /></label>
        <label>n_min <input type="number" style={{ width: 60 }} value={form.n_min} onChange={e => set('n_min', e.target.value)} /></label>
        <label><input type="checkbox" checked={form.veto} onChange={e => set('veto', e.target.checked)} /> 一票否决(veto)</label>
        <label><input type="checkbox" checked={form.enabled} onChange={e => set('enabled', e.target.checked)} /> 启用</label>
      </div>
      <label style={{ display: 'block', margin: '8px 0' }}>机器粗筛 success_condition（JSON）
        <textarea rows={3} style={{ width: '100%', fontFamily: 'monospace' }} value={form.success_condition} onChange={e => set('success_condition', e.target.value)} placeholder='{"type":"evidence_anchor","anchors":["client.exe"],"threshold":1}' />
      </label>
      <label style={{ display: 'block', margin: '8px 0' }}>备注
        <input style={{ width: '100%' }} value={form.note} onChange={e => set('note', e.target.value)} />
      </label>
      <div style={{ marginTop: 10 }}>
        <button className="ghost" onClick={onSubmit}>保存（写回 YAML）</button>
        <button className="ghost" onClick={onCancel}>取消</button>
      </div>
    </div>
  )
}
