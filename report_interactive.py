#!/usr/bin/env python3
"""
report_interactive.py — 交互式 HTML 评测报告（单页 SPA 四级下钻）。

下钻结构：总览 → skill → 会话 → 轮次

数据整合：
  - scanner.scan_all() 提供跨会话聚合 + 逐轮 trace（含 prompt/雪球点）
  - cost.compute_cost() 提供每个会话的成本换算
  - analyze.extract_env_fingerprint() 提供每个会话的环境指纹
  - classify_level.classify_level() 对未匹配 task 的 session 自动推断级别

输出：单文件自包含 HTML，内嵌所有数据 + JS 交互，双击即用。

用法：
    python report_interactive.py --dir <projects目录> --out results/report_interactive.html
"""

import json
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from scanner import scan_all
from cost import compute_cost
from analyze import extract_env_fingerprint
from parser import parse_session_jsonl, replay_metrics
from classify_level import classify_level


# 网易深色风
CATEGORY_COLORS = [
    "#5b8ff9", "#d95926", "#199e70", "#c98500",
    "#d55181", "#9085e9", "#e66767", "#6dc8ec",
]
BRAND_RED = "#D9382B"
SURFACE = "#1a1a2e"
CARD = "#22223b"
INK_PRIMARY = "#e8e8f0"
INK_SECONDARY = "#a0a0b8"
INK_MUTED = "#6b6b85"
GRID = "#2c2c44"

# 全局 task 文件列表
_task_files = []


def fmt_tokens(n):
    if n >= 100_000_000:
        return f"{n/100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n/10_000:.1f} 万"
    return f"{n:,}"


def fmt_duration(seconds):
    """格式化耗时（秒 → 分钟/小时）。"""
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        return f"{seconds/60:.1f} 分钟"
    return f"{seconds/3600:.1f} 小时"


def _count_user_turns(jsonl_path):
    """统计真实用户轮次（排除 / 开头的本地命令）。"""
    turns = 0
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "user":
                c = obj.get("message", {}).get("content", "")
                if isinstance(c, str) and c and not c.startswith("/"):
                    turns += 1
    return turns


def build_data(proj_dir: str, task_files: list = None, use_llm_classify: bool = True) -> dict:
    """整合 scanner + cost + fingerprint + replay_metrics + classify_level。

    task_files: [{task_id, level, note, ...}, ...] 可选，用于给会话补充级别/结论
    use_llm_classify: 是否对未匹配 task 的 session 用 LLM 自动推断级别
    """
    global _task_files
    _task_files = task_files or []

    scan = scan_all(proj_dir)
    summary = scan["summary"]
    sessions = scan["sessions"]

    enriched = []
    for s in sessions:
        # 找到匹配的 task
        matched_task = None
        stem = Path(s["jsonl_path"]).stem
        for tf in _task_files:
            tfd = tf["_data"]
            if stem in tfd.get("note", "") or tfd.get("task_id", "").startswith(stem[:8]):
                matched_task = tfd
                break

        # 基础特征提取（所有 session 都需要）
        try:
            parsed = parse_session_jsonl(s["jsonl_path"])
            tool_dist = {}
            for t in parsed.get("tool_sequence", []):
                name = t["name"]
                tool_dist[name] = tool_dist.get(name, 0) + 1
            human_interventions = tool_dist.get("AskUserQuestion", 0)
            user_turns = _count_user_turns(s["jsonl_path"])
            first_tools = [t["name"] for t in parsed.get("tool_sequence", [])[:10]]
        except Exception:
            tool_dist = {}
            human_interventions = 0
            user_turns = 0
            first_tools = []
            parsed = None

        if matched_task:
            # 有 task → replay_metrics 出完整结论
            try:
                rm = replay_metrics(s["jsonl_path"], matched_task)
                level = rm.get("level", matched_task.get("level"))
                task_success = rm["metrics"].get("task_success")
                evidence_hit = rm["metrics"].get("evidence_hit", [])
                evidence_text = rm["metrics"].get("evidence_text", "")
                level_source = "task"
                level_reason = ""
            except Exception as e:
                print(f"  ⚠ replay_metrics 失败 {stem}: {e}")
                level = matched_task.get("level")
                task_success = None
                evidence_hit = None
                evidence_text = None
                level_source = "task_fallback"
                level_reason = ""
        else:
            # 无 task → LLM 自动推断级别
            if use_llm_classify:
                features = {
                    "total_tokens": s["total_tokens"],
                    "tool_calls_total": sum(tool_dist.values()) if tool_dist else 0,
                    "tool_dist": tool_dist,
                    "user_turns": user_turns,
                    "human_interventions": human_interventions,
                    "skill_loaded": s["skill_loaded"],
                    "snowball_count": s["snowball_count"],
                    "first_tools": first_tools,
                }
                cls_result = classify_level(features, use_llm=True)
                level = cls_result.get("level")
                level_reason = cls_result.get("reason", "")
                level_source = "rules" if "error" in cls_result else "llm"
            else:
                level = None
                level_reason = ""
                level_source = "none"
            task_success = None
            evidence_hit = None
            evidence_text = None

        # 成本
        cost = compute_cost({
            "input_tokens": s["total_input"],
            "cache_read_tokens": s["total_cache_read"],
            "cache_write_tokens": 0,
            "output_tokens": s["total_output"],
        })

        # 环境指纹
        if parsed:
            fp = extract_env_fingerprint(s["jsonl_path"], parsed)
        else:
            fp = {"model": s["model"], "skill_count": 0, "mcp_tools_used": 0, "distinct_tools_used": 0}

        enriched.append({
            "session_id": s["session_id"],
            "total_tokens": s["total_tokens"],
            "model": s["model"],
            "skill_loaded": s["skill_loaded"],
            "trace_count": s["trace_count"],
            "snowball_count": s["snowball_count"],
            "level": level,
            "level_source": level_source,
            "level_reason": level_reason,
            "task_success": task_success,
            "evidence_hit": evidence_hit,
            "evidence_text": evidence_text,
            "cost": cost,
            "fingerprint": fp,
            "tool_dist": tool_dist,
            "human_interventions": human_interventions,
            "user_turns": user_turns,
            "duration_s": s.get("duration_s", 0),
            "task_list": s.get("task_list", []),
            "traces": s["traces"],
            "anomalies": s["anomalies"],
        })

    # 按 skill 分组
    skill_map = {}
    for e in enriched:
        sk = e["skill_loaded"] or "（纯agent）"
        skill_map.setdefault(sk, {"sessions": []})
        skill_map[sk]["sessions"].append(e)

    skills = []
    for sk, d in skill_map.items():
        sessions_of_skill = d["sessions"]
        total_tok = sum(x["total_tokens"] for x in sessions_of_skill)
        skills.append({
            "skill": sk,
            "session_count": len(sessions_of_skill),
            "total_tokens": total_tok,
            "snowball_count": sum(x["snowball_count"] for x in sessions_of_skill),
            "sessions": sessions_of_skill,
        })
    skills.sort(key=lambda x: -x["total_tokens"])

    return {
        "summary": summary,
        "skills": skills,
        "sessions": enriched,
    }


# ===== JS 模板 =====

JS_TEMPLATE = r"""
(function() {
  let DATA, COLORS;
  try {
    DATA = JSON.parse(document.getElementById('data-store').value);
    COLORS = ["#5b8ff9","#d95926","#199e70","#c98500","#d55181","#9085e9","#e66767","#6dc8ec"];
  } catch(e) {
    const el = document.getElementById('error-display');
    el.style.display = 'block';
    el.textContent = 'DATA parse error: ' + e.message;
    return;
  }
  window.DATA = DATA;
  window.COLORS = COLORS;

  function fmtT(n) {
    if (n >= 100000000) return (n/100000000).toFixed(2) + ' 亿';
    if (n >= 10000) return (n/10000).toFixed(1) + ' 万';
    return n.toLocaleString();
  }

  function fmtDur(seconds) {
    if (seconds < 60) return seconds.toFixed(1) + ' 秒';
    if (seconds < 3600) return (seconds/60).toFixed(1) + ' 分钟';
    return (seconds/3600).toFixed(1) + ' 小时';
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  window.showView = function(name) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById('view-' + name).classList.add('active');
    // 更新面包屑
    const bc = document.getElementById('breadcrumb');
    if (name === 'overview') bc.textContent = '总览';
  };

  // ===== 总览 =====
  window.renderOverview = function() {
    const s = DATA.summary;
    const el = document.getElementById('view-overview');
    let html = '<div class="stats">' +
      '<div class="stat"><div class="stat-label">会话总数</div><div class="stat-value">' + s.session_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value" style="color:#D9382B">' + fmtT(s.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">雪球会话</div><div class="stat-value">' + s.snowball_sessions + '</div></div>' +
      '<div class="stat"><div class="stat-label">平均 Token/会话</div><div class="stat-value">' + fmtT(s.avg_tokens) + '</div></div>' +
    '</div>';

    // Skill 分布
    html += '<div class="panel"><h3>按 Skill 分布</h3>';
    const maxV = DATA.skills.length ? DATA.skills[0].total_tokens : 1;
    DATA.skills.forEach((sk, i) => {
      const w = Math.max(sk.total_tokens / maxV * 100, 1);
      const displayName = sk.skill.length > 14 ? sk.skill.slice(0,14) + '…' : sk.skill;
      html += '<div class="bar-row" onclick="renderSkill(' + i + ')">' +
        '<div class="bar-label" title="' + esc(sk.skill) + '">' + esc(displayName) + '</div>' +
        '<div class="bar-track"><div class="bar-fill" style="width:' + w + '%;background:' + COLORS[i % COLORS.length] + '"></div></div>' +
        '<div class="bar-val">' + fmtT(sk.total_tokens) + ' · ' + sk.session_count + ' 会话</div>' +
      '</div>';
    });
    html += '</div>';

    // Top 会话
    html += '<div class="panel"><h3>Top 消耗会话</h3><table><thead>' +
      '<tr><th>Session</th><th>Token</th><th>模型</th><th>Skill</th><th>级别</th><th>雪球</th></tr></thead><tbody>';
    DATA.sessions.slice(0, 20).forEach((sess) => {
      const lvl = sess.level ? '<span class="tag" style="font-size:10px">' + esc(sess.level) + '</span>' : '<span class="muted">-</span>';
      html += '<tr>' +
        '<td><code>' + esc(sess.session_id.slice(0,8)) + '</code></td>' +
        '<td class="num">' + fmtT(sess.total_tokens) + '</td>' +
        '<td>' + esc(sess.model) + '</td>' +
        '<td>' + esc(sess.skill_loaded || '纯agent') + '</td>' +
        '<td>' + lvl + '</td>' +
        '<td class="num"><span class="' + (sess.snowball_count > 0 ? 'snowball-dot' : 'muted') + '">' + sess.snowball_count + '</span></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';

    el.innerHTML = html;
    document.getElementById('breadcrumb').textContent = '总览';
  };

  // ===== Skill =====
  window.renderSkill = function(idx) {
    const sk = DATA.skills[idx];
    const el = document.getElementById('view-skill');
    let html = '<button class="back-btn" onclick="window.showView(\'overview\');renderOverview()">← 返回总览</button>';
    html += '<h3>' + esc(sk.skill) + '</h3>';
    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">会话数</div><div class="stat-value">' + sk.session_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value" style="color:#D9382B">' + fmtT(sk.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">雪球点</div><div class="stat-value">' + sk.snowball_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">平均/会话</div><div class="stat-value">' + fmtT(Math.round(sk.total_tokens / sk.session_count)) + '</div></div>' +
    '</div>';

    // 级别分布
    const lvlCounts = {};
    sk.sessions.forEach(s => { const l = s.level || '?'; lvlCounts[l] = (lvlCounts[l]||0) + 1; });
    html += '<div class="panel"><h3>级别分布</h3>';
    Object.entries(lvlCounts).sort().forEach(([l, c]) => {
      html += '<span class="tag" style="margin-right:8px">' + esc(l) + ' ×' + c + '</span>';
    });
    html += '</div>';

    html += '<div class="panel"><table><thead><tr><th>Session</th><th>Token</th><th>成本</th><th>模型</th><th>级别</th><th>工具数</th><th>雪球</th></tr></thead><tbody>';
    sk.sessions.forEach((sess, i) => {
      const lvl = sess.level ? '<span class="tag" style="font-size:10px">' + esc(sess.level) + '</span>' : '-';
      html += '<tr class="clickable" onclick="renderSession(' + idx + ',' + i + ')">' +
        '<td><code>' + esc(sess.session_id.slice(0,8)) + '</code></td>' +
        '<td class="num">' + fmtT(sess.total_tokens) + '</td>' +
        '<td class="num">¥' + sess.cost.total_cost.toFixed(2) + '</td>' +
        '<td>' + esc(sess.model) + '</td>' +
        '<td>' + lvl + '</td>' +
        '<td class="num">' + Object.keys(sess.tool_dist).length + '</td>' +
        '<td class="num"><span class="' + (sess.snowball_count > 0 ? 'snowball-dot' : 'muted') + '">' + sess.snowball_count + '</span></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
    document.getElementById('breadcrumb').textContent = '总览 › ' + sk.skill;
    window.showView('skill');
  };

  // ===== 会话 =====
  window.renderSession = function(skillIdx, sessIdx) {
    const sess = DATA.skills[skillIdx].sessions[sessIdx];
    const el = document.getElementById('view-session');
    let html = '<button class="back-btn" onclick="renderSkill(' + skillIdx + ')">← 返回 ' + esc(DATA.skills[skillIdx].skill) + '</button>';
    html += '<h3><code>' + esc(sess.session_id) + '</code></h3>';

    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value">' + fmtT(sess.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">成本</div><div class="stat-value" style="color:#D9382B">¥' + sess.cost.total_cost.toFixed(2) + '</div></div>' +
      '<div class="stat"><div class="stat-label">Trace 数</div><div class="stat-value">' + sess.trace_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">人工介入</div><div class="stat-value">' + (sess.human_interventions || 0) + ' 次</div></div>' +
    '</div>';

    // 耗时统计
    if (sess.duration_s && sess.duration_s > 0) {
      html += '<div class="panel"><h3>耗时统计</h3>';
      html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">';
      html += '<div class="stat"><div class="stat-label">总耗时</div><div class="stat-value">' + fmtDur(sess.duration_s) + '</div></div>';
      html += '<div class="stat"><div class="stat-label">平均/轮</div><div class="stat-value">' + fmtDur(sess.duration_s / Math.max(sess.trace_count, 1)) + '</div></div>';
      html += '<div class="stat"><div class="stat-label">用户轮次</div><div class="stat-value">' + (sess.user_turns || 0) + ' 轮</div></div>';
      html += '</div></div>';
    }

    // Task List
    if (sess.task_list && sess.task_list.length > 0) {
      html += '<div class="panel"><h3>Task List 执行追踪</h3>';
      html += '<table><thead><tr><th>Task</th><th>状态</th><th>耗时</th></tr></thead><tbody>';
      sess.task_list.forEach(task => {
        const statusIcon = task.status === 'completed' ? '✅' : task.status === 'in_progress' ? '🔄' : task.status === 'blocked' ? '⛔' : '⏳';
        const dur = task.duration_s ? fmtDur(task.duration_s) : '-';
        html += '<tr>' +
          '<td><strong>' + esc(task.subject) + '</strong><br><span class="muted" style="font-size:11px">' + esc(task.description.slice(0, 80)) + '</span></td>' +
          '<td>' + statusIcon + ' ' + esc(task.status) + '</td>' +
          '<td class="num">' + dur + '</td>' +
        '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // 级别
    if (sess.level) {
      const lvlDesc = {L1:'简单单一动作', L2:'简单动作组合', L3:'混合真实场景', L4:'不可能/负面任务'}[sess.level] || '';
      const srcTag = {task:'task文件', llm:'LLM推断', rules:'规则推断', task_fallback:'task(回退)', none:'无'}[sess.level_source] || '';
      html += '<div class="panel"><h3>任务级别</h3>';
      html += '<span class="tag">' + esc(sess.level) + '</span>';
      html += ' <span class="muted" style="margin-left:8px">' + esc(lvlDesc) + '</span>';
      html += ' <span class="muted" style="margin-left:8px;font-size:11px">(' + esc(srcTag) + ')</span>';
      if (sess.level_reason) {
        html += '<br><span class="muted" style="font-size:12px">' + esc(sess.level_reason) + '</span>';
      }
      html += '</div>';
    }

    // 结论
    if (sess.task_success !== null && sess.task_success !== undefined) {
      const ok = sess.task_success;
      html += '<div class="panel"><h3>评测结论</h3>';
      html += '<span style="font-weight:600;color:' + (ok ? '#5ad8a6' : '#e66767') + '">' + (ok ? '✅ 成功' : '❌ 失败') + '</span>';
      if (sess.evidence_hit && sess.evidence_hit.length) {
        html += '<br><span class="muted" style="font-size:12px">命中锚点: ' + sess.evidence_hit.map(esc).join('、') + '</span>';
      }
      if (sess.evidence_text) {
        html += '<div class="prompt-box" style="margin-top:8px">' + esc(sess.evidence_text) + '</div>';
      }
      html += '</div>';
    }

    // 成本分项
    html += '<div class="panel"><h3>成本分项</h3><table>' +
      '<tr><td>输入</td><td class="num">¥' + sess.cost.input_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>缓存读</td><td class="num">¥' + sess.cost.cache_read_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>缓存写</td><td class="num">¥' + sess.cost.cache_write_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>输出</td><td class="num">¥' + sess.cost.output_cost.toFixed(2) + '</td></tr>' +
    '</table></div>';

    // 环境指纹
    html += '<div class="panel"><h3>环境指纹</h3>';
    html += '<span class="muted">模型 ' + esc(sess.fingerprint.model) + ' · skill数 ' + (sess.fingerprint.skill_count||0) +
            ' · mcp工具数 ' + (sess.fingerprint.mcp_tools_used||0) + ' · 工具种类 ' + (sess.fingerprint.distinct_tools_used||0) + '</span></div>';

    // 工具分布
    const tools = Object.entries(sess.tool_dist).sort((a,b) => b[1]-a[1]);
    html += '<div class="panel"><h3>工具调用分布</h3><table><thead><tr><th>工具</th><th>次数</th></tr></thead><tbody>';
    tools.slice(0, 20).forEach(([name, cnt]) => {
      html += '<tr><td><code>' + esc(name) + '</code></td><td class="num">' + cnt + '</td></tr>';
    });
    html += '</tbody></table></div>';

    // 趋势图
    if (sess.traces && sess.traces.length) {
      html += '<div class="panel"><h3>逐轮 Token 趋势（点击红点看轮次详情）</h3>';
      html += window.trendSVG(sess.traces, skillIdx, sessIdx);
      html += '</div>';
    }

    el.innerHTML = html;
    document.getElementById('breadcrumb').textContent = '总览 › ' + DATA.skills[skillIdx].skill + ' › ' + sess.session_id.slice(0,8);
    window.showView('session');
  };

  // ===== 趋势图 =====
  window.trendSVG = function(traces, skillIdx, sessIdx) {
    const W = 760, H = 200, padL = 45, padR = 15, padT = 10, padB = 30;
    let pts = traces;
    if (pts.length > 80) {
      const step = Math.ceil(pts.length / 80);
      pts = pts.filter((_, i) => i % step === 0 || i === pts.length - 1);
    }
    const maxTok = Math.max(...pts.map(t => t.round_tokens || 0), 1);
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const n = pts.length;
    const px = i => padL + (i / Math.max(n-1, 1)) * plotW;
    const py = v => padT + plotH - (v / maxTok) * plotH;

    let path = '', area = '', dots = '', ticks = '';
    pts.forEach((t, i) => {
      path += (i ? ' L ' : '') + px(i).toFixed(1) + ',' + py(t.round_tokens || 0).toFixed(1);
    });
    area = px(0).toFixed(1) + ',' + (padT + plotH).toFixed(1) + ' L ' + path + ' L ' + px(n-1).toFixed(1) + ',' + (padT + plotH).toFixed(1) + ' Z';

    pts.forEach((t, i) => {
      if (t.is_snowball) {
        dots += '<circle cx="' + px(i).toFixed(1) + '" cy="' + py(t.round_tokens || 0).toFixed(1) + '" r="6" fill="#D9382B" style="cursor:pointer" onclick="renderTurn(' + skillIdx + ',' + sessIdx + ',' + t.round + ')"><title>第' + t.round + '轮 雪球</title></circle>';
      }
    });
    for (let i = 0; i < 3; i++) {
      const v = maxTok * i / 2, yy = py(v);
      ticks += '<text x="' + (padL - 6) + '" y="' + yy.toFixed(1) + '" fill="#6b6b85" font-size="9" text-anchor="end">' + fmtT(Math.round(v)) + '</text>';
      ticks += '<line x1="' + padL + '" y1="' + yy.toFixed(1) + '" x2="' + (W - padR) + '" y2="' + yy.toFixed(1) + '" stroke="#2c2c44" stroke-width="0.5"/>';
    }
    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '">' + ticks +
      '<path d="' + area + '" fill="' + COLORS[0] + '" opacity="0.08"/>' +
      '<path d="M ' + path + '" fill="none" stroke="' + COLORS[0] + '" stroke-width="2"/>' + dots +
      '<text x="' + (W - padR) + '" y="' + (H - 8) + '" fill="#6b6b85" font-size="9" text-anchor="end">轮次</text></svg>';
  };

  // ===== 轮次 =====
  window.renderTurn = function(skillIdx, sessIdx, roundNum) {
    const sess = DATA.skills[skillIdx].sessions[sessIdx];
    const tr = sess.traces.find(t => t.round === roundNum);
    if (!tr) {
      const el = document.getElementById('error-display');
      el.style.display = 'block';
      el.textContent = '未找到第 ' + roundNum + ' 轮';
      return;
    }
    const el = document.getElementById('view-turn');
    let html = '<button class="back-btn" onclick="renderSession(' + skillIdx + ',' + sessIdx + ')">← 返回会话</button>';
    html += '<h3>第 ' + tr.round + ' 轮详情</h3>';
    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">该轮 Token</div><div class="stat-value">' + fmtT(tr.round_tokens || 0) + '</div></div>' +
      '<div class="stat"><div class="stat-label">增量</div><div class="stat-value" style="color:' + (tr.is_snowball ? '#D9382B' : '#e8e8f0') + '">' + fmtT(tr.increment || 0) + '</div></div>' +
      '<div class="stat"><div class="stat-label">是否雪球</div><div class="stat-value">' + (tr.is_snowball ? '🔴 是' : '否') + '</div></div>' +
      '<div class="stat"><div class="stat-label">模型</div><div class="stat-value" style="font-size:14px">' + esc(tr.model) + '</div></div>' +
    '</div>';
    if (tr.prompt) {
      html += '<div class="panel"><h3>触发该轮的 Prompt</h3><div class="prompt-box">' + esc(tr.prompt) + '</div></div>';
    }
    el.innerHTML = html;
    document.getElementById('breadcrumb').textContent = '总览 › ' + DATA.skills[skillIdx].skill + ' › 第' + tr.round + '轮';
    window.showView('turn');
  };

  window.onerror = function(msg, src, line, col) {
    const el = document.getElementById('error-display');
    if (el) { el.style.display = 'block'; el.textContent = 'JS错误: ' + msg + ' at ' + src + ':' + line; }
  };

  try { renderOverview(); } catch(e) {
    const el = document.getElementById('error-display');
    el.style.display = 'block';
    el.textContent = 'renderOverview 失败: ' + e.message;
  }
})();
"""


def render_html(data: dict) -> str:
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>evalkit 交互式评测报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ background-color: {SURFACE}; color: {INK_PRIMARY}; font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 14px; line-height: 1.6; margin: 0; padding: 24px 20px; }}
  .container {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }}
  h1::before {{ content: ""; width: 6px; height: 22px; background: {BRAND_RED}; border-radius: 2px; }}
  h3 {{ font-size: 15px; color: {INK_PRIMARY}; margin: 0 0 12px; font-weight: 600; }}
  .sub {{ color: {INK_SECONDARY}; margin: 0 0 20px; font-size: 13px; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0 24px; }}
  .stat {{ background: {CARD}; border-radius: 8px; padding: 14px 16px; }}
  .stat-label {{ color: {INK_MUTED}; font-size: 12px; margin-bottom: 4px; }}
  .stat-value {{ font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .panel {{ background: {CARD}; border-radius: 8px; padding: 18px; margin-bottom: 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: {INK_MUTED}; font-weight: normal; border-bottom: 1px solid {GRID}; padding: 7px 10px; }}
  td {{ border-bottom: 1px solid #2a2a42; padding: 9px 10px; color: {INK_SECONDARY}; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.clickable {{ cursor: pointer; }}
  tr.clickable:hover {{ background: #2a2a46; }}
  code {{ background: #2d2d46; padding: 2px 6px; border-radius: 3px; font-family: Consolas, monospace; font-size: 12px; color: {INK_PRIMARY}; }}
  .num {{ font-variant-numeric: tabular-nums; color: {INK_PRIMARY}; }}
  .muted {{ color: {INK_MUTED}; }}
  .back-btn {{ display: inline-block; background: rgba(91,143,249,0.12); border: 1px solid rgba(91,143,249,0.4); color: #5b8ff9; border-radius: 6px; padding: 4px 14px; cursor: pointer; font-size: 13px; margin: 0 0 14px; }}
  .back-btn:hover {{ background: rgba(91,143,249,0.22); }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin: 6px 0; cursor: pointer; }}
  .bar-row:hover {{ opacity: 0.85; }}
  .bar-label {{ width: 140px; font-size: 12px; color: {INK_SECONDARY}; text-align: right; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar-track {{ flex: 1; height: 22px; background: #2a2a42; border-radius: 4px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; }}
  .bar-val {{ width: 70px; font-size: 12px; color: {INK_PRIMARY}; font-variant-numeric: tabular-nums; }}
  .snowball-dot {{ color: {BRAND_RED}; font-weight: 600; }}
  .tag {{ display: inline-block; background: rgba(217,56,43,0.15); color: {BRAND_RED}; border: 1px solid rgba(217,56,43,0.4); border-radius: 4px; padding: 1px 7px; font-size: 11px; }}
  .view {{ display: none; }}
  .view.active {{ display: block; }}
  .prompt-box {{ background: #252540; border-left: 3px solid #5b8ff9; padding: 8px 12px; margin: 8px 0; font-size: 13px; color: #ccc; border-radius: 0 4px 4px 0; white-space: pre-wrap; }}
  #error-display {{ background: #3d1a1a; border: 1px solid {BRAND_RED}; color: #ff6b6b; padding: 16px; border-radius: 8px; margin: 16px 0; font-family: Consolas, monospace; font-size: 13px; white-space: pre-wrap; display: none; }}
</style>
</head>
<body>
<div class="container">
  <h1>evalkit 交互式评测报告</h1>
  <p class="sub" id="breadcrumb">总览</p>
  <textarea id="data-store" style="display:none">{data_json}</textarea>
  <div id="error-display"></div>
  <div class="view active" id="view-overview"></div>
  <div class="view" id="view-skill"></div>
  <div class="view" id="view-session"></div>
  <div class="view" id="view-turn"></div>
</div>
<script>
{JS_TEMPLATE}
</script>
</body>
</html>"""
    return html


def main():
    ap = argparse.ArgumentParser(description="生成交互式 HTML 评测报告")
    ap.add_argument("--dir", required=True, help="projects 目录")
    ap.add_argument("--tasks", default="tasks", help="task 文件目录")
    ap.add_argument("--out", default="results/report_interactive.html", help="输出 HTML 路径")
    ap.add_argument("--no-llm", action="store_true", help="禁用 LLM 推断级别，只用规则")
    args = ap.parse_args()

    task_files = []
    tasks_dir = Path(__file__).parent / args.tasks
    if tasks_dir.exists():
        for tf in tasks_dir.glob("*.json"):
            try:
                td = json.loads(tf.read_text(encoding="utf-8"))
                task_files.append({"_file": str(tf), "_data": td})
            except Exception as e:
                print(f"  ⚠ 跳过 {tf.name}: {e}")
    print(f"加载 {len(task_files)} 个 task 文件")

    print("扫描并整合数据...")
    data = build_data(args.dir, task_files, use_llm_classify=not args.no_llm)
    print(f"  共 {data['summary']['session_count']} 个会话, {len(data['skills'])} 个 skill")

    # 统计级别分布
    lvl_counts = {}
    for s in data["sessions"]:
        l = s.get("level") or "?"
        lvl_counts[l] = lvl_counts.get(l, 0) + 1
    print("  级别分布:", ", ".join(f"{k}×{v}" for k, v in sorted(lvl_counts.items())))

    print("渲染 HTML...")
    html = render_html(data)

    out = Path(__file__).parent / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"交互式报告已生成: {out}")


if __name__ == "__main__":
    main()
