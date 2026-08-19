// evalkit 看板 API 封装（对接 eval_server）
const BASE = ''

export async function getSessions(scope = 'all') {
  const r = await fetch(`${BASE}/api/sessions?scope=${encodeURIComponent(scope)}`)
  const j = await r.json()
  return j.sessions || []
}

export async function getAgentStatus() {
  const r = await fetch(`${BASE}/api/agent-status`)
  const j = await r.json()
  return j.agents || {}
}

export async function getProviders() {
  const r = await fetch(`${BASE}/api/providers`)
  const j = await r.json()
  return j.providers || []
}

export async function startEval(params) {
  const r = await fetch(`${BASE}/api/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return r.json()
}

export async function stopEval() {
  const r = await fetch(`${BASE}/api/stop`, { method: 'POST' })
  return r.json()
}

export async function launchTerminal(params) {
  const r = await fetch(`${BASE}/api/terminal/launch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  })
  return r.json()
}

export async function stopTerminal(pid) {
  const r = await fetch(`${BASE}/api/terminal/stop`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pid }),
  })
  return r.json()
}

export async function listTerminals() {
  const r = await fetch(`${BASE}/api/terminals`)
  const j = await r.json()
  return j.terminals || []
}

export async function attach(session) {
  const r = await fetch(`${BASE}/api/attach`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: session.session_id,
      agent: session.agent,
      path: session.path,
      mode: session.state === 'live' ? 'live' : 'replay',
    }),
  })
  return r.json()
}

export async function detach(sessionId) {
  await fetch(`${BASE}/api/detach`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  })
}

export async function addSessionPath(path) {
  const r = await fetch(`${BASE}/api/sessions/add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  return r.json()
}

export async function renameSession(sessionId, name) {
  const r = await fetch(`${BASE}/api/sessions/rename`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, name }),
  })
  return r.json()
}

export async function removeSession(sessionId) {
  const r = await fetch(`${BASE}/api/sessions/remove`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  })
  return r.json()
}

export async function getRaw(sessionId) {
  const r = await fetch(`${BASE}/api/raw?session_id=${encodeURIComponent(sessionId)}`)
  if (!r.ok) return null
  return r.text()
}

export async function listFs(path) {
  const r = await fetch(`${BASE}/api/fs?path=${encodeURIComponent(path || '')}`)
  return r.json()
}

export async function getStats() {
  const r = await fetch(`${BASE}/api/stats`)
  const j = await r.json()
  return j.rows || []
}

export async function getExecutions(taskId, model) {
  const q = `task_id=${encodeURIComponent(taskId || '')}&model=${encodeURIComponent(model || '')}`
  const r = await fetch(`${BASE}/api/executions?${q}`)
  const j = await r.json()
  return j.executions || []
}

export async function reviewExecution(sessionId, patch) {
  const r = await fetch(`${BASE}/api/executions/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  return r.json()
}

export async function getTasks() {
  const r = await fetch(`${BASE}/api/tasks`)
  const j = await r.json()
  return j.tasks || []
}

export async function getReferences() {
  const r = await fetch(`${BASE}/api/references`)
  const j = await r.json()
  return j.references || {}
}

export async function saveTask(task) {
  const r = await fetch(`${BASE}/api/tasks`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(task),
  })
  return r.json()
}

export async function deleteTask(taskId) {
  const r = await fetch(`${BASE}/api/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
  return r.json()
}

export async function generateTasks(params) {
  const r = await fetch(`${BASE}/api/tasks/generate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params),
  })
  return r.json()
}

export async function fetchDkDevices(params) {
  const r = await fetch(`${BASE}/api/dk/devices`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params),
  })
  return r.json()
}

export async function startBatch(params) {
  const r = await fetch(`${BASE}/api/batch/start`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(params),
  })
  return r.json()
}

export async function stopBatch() {
  const r = await fetch(`${BASE}/api/batch/stop`, { method: 'POST' })
  return r.json()
}

export async function getBatchStatus() {
  const r = await fetch(`${BASE}/api/batch/status`)
  return r.json()
}

export async function getEnv(skill) {
  const r = await fetch(`${BASE}/api/env?skill=${encodeURIComponent(skill || '')}`)
  return r.json()
}

export async function getModels() {
  const r = await fetch(`${BASE}/api/models`)
  const j = await r.json()
  return j.models || []
}

export function connectSSE(onFrame, onStatus) {
  const es = new EventSource(`${BASE}/events`)
  es.onopen = () => onStatus && onStatus('connected')
  es.onmessage = ev => {
    let f
    try { f = JSON.parse(ev.data) } catch { return }
    onFrame(f)
  }
  es.onerror = () => {
    onStatus && onStatus('reconnecting')
    es.close()
    setTimeout(() => connectSSE(onFrame, onStatus), 1500)
  }
  return es
}
