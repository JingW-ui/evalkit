#!/usr/bin/env python3
"""
session_report.py — 单会话深度评测 HTML 报告。

与 report_interactive.py（跨会话总览 + 四级下钻）不同，这里把**单个会话**做深做细：
  - 任务按「真实用户对话」切分（一条不以 / 或 < 开头的 type:user 消息 = 一个任务）
  - 噪音（/clear、local-command-caveat、ai-title、last-prompt 等）自动忽略
  - 每个任务独立统计：query、起止时间、耗时、skill 触发、token 增量、工具调用、嵌套子任务
  - TaskCreate/TaskUpdate 关联：TaskCreate 常不带 id，靠出现顺序配对 TaskUpdate 的数字 id
  - 阻塞判定：只看真实 status == "blocked"，不虚构阈值阻塞

用法：
    python session_report.py --jsonl <单个 .jsonl 路径> [--out results/session_report.html]
"""

import json
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone


# ===== 颜色主题（GitHub 风格：绿色为主，仅少量红色点缀） =====
ACCENT = "#2da44e"        # GitHub 绿，主强调色
ACCENT_DIM = "#238636"    # 绿（深一档，用于 hover/次要）
RED = "#cf222e"           # 仅少量点缀：错误/阻塞等负面信号
SURFACE = "#0d1117"       # GitHub dark 背景
CARD = "#161b22"          # 卡片/面板
INK_PRIMARY = "#e6edf3"
INK_SECONDARY = "#8b949e"
INK_MUTED = "#6e7681"
GRID = "#30363d"

# 匹配工具参数里引用 skill 目录的路径：.../skills/<名>/... （用于「脚本直用」检测）
_SKILL_DIR_RE = re.compile(r"skills[/\\]([A-Za-z0-9._-]+)[/\\]")


def _parse_ts(ts):
    """ISO 时间戳字符串 → epoch 秒；失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _is_real_user(content):
    """判定一条 user 消息是否为真实任务指令（排除 / 开头、< 开头、空）。"""
    if not isinstance(content, str):
        return False
    c = content.strip()
    if not c:
        return False
    if c.startswith("/") or c.startswith("<"):
        return False
    return True


def _fmt_tokens(n):
    """token 数人类可读：>1亿 → 亿；>1万 → 万；否则千分位。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万"
    return f"{n:,}"


def _fmt_duration(seconds):
    """秒 → 人类可读时长。"""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m} 分 {s} 秒"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h} 小时 {m} 分"


# ===== 核心解析 =====

def scan_single_session(jsonl_path):
    """
    解析单个 Claude Code session JSONL。

    Returns:
        dict：{session_id, model_usage, tool_dist, token 汇总, tasks[], task_subitems[], blocks, ...}
    """
    path = Path(jsonl_path)
    session_id = path.stem

    # 累积量
    model_usage = {}        # model -> 轮数
    tool_dist = {}          # 工具名 -> 次数
    tokens = {
        "input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
    }
    ts_first = None
    ts_last = None

    tasks = []              # 真实用户任务（按对话切分）
    cur_task = None         # 当前任务指针

    # TaskCreate/TaskUpdate 关联：TaskCreate 常不带 id，按出现顺序配对数字 id
    creates = []            # 出现顺序的 TaskCreate（subject/description/...）
    updates = []            # 所有 TaskUpdate（taskId, status, ts）
    skill_events = []       # {skill_name, ts}

    # 人工介入（AskUserQuestion）+ 任务完成判定
    human_interventions = []     # {header, question, options, ts}
    stop_reasons = []            # 有序 stop_reason（含时间戳）
    final_texts = []             # 每轮 assistant 的最终 text 合并（用于完成判定）

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type", "")
            ts = obj.get("timestamp", "")

            # 时间线
            if ts:
                if ts_first is None:
                    ts_first = ts
                ts_last = ts

            # 真实用户任务：新任务起点
            if t == "user":
                content = obj.get("message", {}).get("content", "")
                if _is_real_user(content):
                    # 结束上一个任务的耗时
                    if cur_task is not None:
                        cur_task["end_ts"] = ts
                    cur_task = {
                        "query": content.strip(),
                        "start_ts": ts,
                        "end_ts": None,
                        "tool_calls": 0,
                        "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                        "skill_loaded": None,        # 显式 Skill 工具加载的 skill 名（首次）
                        "skill_count": 0,
                        "skill_via_script": None,    # 直接 Bash/Read 引用 skills/<名>/ 目录判定的 skill 名（首次）
                        "skill_via_script_count": 0,
                        "human_interventions": 0,    # 该任务内 AskUserQuestion 次数
                        "stop_reason": None,         # 该任务最后一条 assistant 的 stop_reason
                        "has_final_text": False,     # 该任务是否有最终 text 输出（非工具调用）
                    }
                    tasks.append(cur_task)

            elif t == "assistant":
                msg = obj.get("message", {})
                model = msg.get("model", "")
                if model:
                    model_usage[model] = model_usage.get(model, 0) + 1

                # stop_reason + 最终 text（用于任务完成判定）
                sr = msg.get("stop_reason", "")
                stop_reasons.append({"stop_reason": sr, "ts": ts})
                if cur_task is not None:
                    cur_task["stop_reason"] = sr
                # 看这轮有没有 text block（非工具调用）
                for blk in msg.get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text", "").strip():
                        if cur_task is not None:
                            cur_task["has_final_text"] = True
                        break

                usage = msg.get("usage", {})
                ui = usage.get("input_tokens", 0)
                ur = usage.get("cache_read_input_tokens", 0)
                uw = usage.get("cache_creation_input_tokens", 0)
                uo = usage.get("output_tokens", 0)
                tokens["input"] += ui
                tokens["cache_read"] += ur
                tokens["cache_write"] += uw
                tokens["output"] += uo

                # 归到当前任务
                if cur_task is not None:
                    cur_task["tokens"]["input"] += ui
                    cur_task["tokens"]["cache_read"] += ur
                    cur_task["tokens"]["cache_write"] += uw
                    cur_task["tokens"]["output"] += uo

                # 工具调用
                for blk in msg.get("content", []):
                    if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
                        continue
                    name = blk.get("name", "")
                    if not name:
                        continue
                    tool_dist[name] = tool_dist.get(name, 0) + 1
                    if cur_task is not None:
                        cur_task["tool_calls"] += 1

                    inp = blk.get("input", {})

                    if name == "Skill":
                        sk = inp.get("skill", "") or "（未命名）"
                        skill_events.append({"skill_name": sk, "ts": ts})
                        if cur_task is not None:
                            cur_task["skill_count"] += 1
                            if cur_task["skill_loaded"] is None:
                                cur_task["skill_loaded"] = sk
                    else:
                        # 脚本直用检测：任意工具参数里引用 skills/<名>/ 目录
                        # 说明 agent 绕开 Skill 工具，直接执行了某 skill 目录下的脚本
                        inp_str = json.dumps(inp, ensure_ascii=False)
                        m = _SKILL_DIR_RE.search(inp_str)
                        if m:
                            sk_name = m.group(1)
                            if cur_task is not None:
                                cur_task["skill_via_script_count"] += 1
                                if cur_task["skill_via_script"] is None:
                                    cur_task["skill_via_script"] = sk_name

                    if name == "AskUserQuestion":
                        # 人工介入检测：agent 中途反向追问用户
                        qs = inp.get("questions", [])
                        for q in qs:
                            opts = [o.get("label", "") for o in q.get("options", [])]
                            human_interventions.append({
                                "header": q.get("header", ""),
                                "question": q.get("question", ""),
                                "options": opts,
                                "ts": ts,
                            })
                        if cur_task is not None:
                            cur_task["human_interventions"] += len(qs) if qs else 1

                    if name == "TaskCreate":
                        creates.append({
                            "subject": inp.get("subject", ""),
                            "description": inp.get("description", ""),
                            "ts": ts,
                        })

                    elif name == "TaskUpdate":
                        updates.append({
                            "task_id": inp.get("taskId", inp.get("id", "")),
                            "status": inp.get("status", ""),
                            "ts": ts,
                        })

    # 收尾：最后一个任务
    if cur_task is not None:
        cur_task["end_ts"] = ts_last

    # ===== TaskCreate/TaskUpdate 配对（顺序对应） =====
    # TaskCreate 无 id，TaskUpdate 的 task_id 是 "1"~"N"，按出现顺序一一对应
    update_by_order = []
    for u in updates:
        try:
            order = int(str(u["task_id"]))
        except (ValueError, TypeError):
            order = None
        update_by_order.append((order, u))

    task_subitems = []
    for idx, c in enumerate(creates):
        # 第 idx 个子任务（0-based）对应 taskId = idx+1
        target_order = idx + 1
        status = "pending"
        start_ts = c["ts"]
        completed_ts = None
        for order, u in update_by_order:
            if order == target_order:
                if u["status"] == "in_progress":
                    status = "in_progress"
                    start_ts = u["ts"]
                elif u["status"] == "completed":
                    status = "completed"
                    completed_ts = u["ts"]
                elif u["status"] == "blocked":
                    status = "blocked"

        dur = None
        if start_ts and completed_ts:
            t0 = _parse_ts(start_ts)
            t1 = _parse_ts(completed_ts)
            if t0 is not None and t1 is not None:
                dur = t1 - t0

        # 归属：找到起始时间之后的第一个用户任务
        bel = None
        c_ts = _parse_ts(c["ts"])
        for tk in tasks:
            s_ts = _parse_ts(tk["start_ts"])
            if s_ts is not None and c_ts is not None:
                dif = s_ts - c_ts
                # 任务在 create 之前或 create 之后最近的那个
                if s_ts <= c_ts:  # create 发生在任务开始之后
                    bel = tk["query"]
                else:
                    break

        task_subitems.append({
            "subject": c["subject"],
            "description": c["description"],
            "status": status,
            "duration_s": dur,
            "belongs_to": bel,
            "created_ts": c["ts"],
        })

    # ===== 汇总 =====
    total_tool_calls = sum(tool_dist.values())
    skill_triggered_tasks = sum(1 for tk in tasks if tk["skill_count"] > 0)
    skill_script_tasks = sum(1 for tk in tasks if tk["skill_via_script_count"] > 0)
    # 任一方式（显式 Skill 工具 或 脚本直用）都算「使用了 skill」
    skill_used_tasks = sum(
        1 for tk in tasks if (tk["skill_count"] > 0 or tk["skill_via_script_count"] > 0)
    )
    blocks = [s for s in task_subitems if s["status"] == "blocked"]
    completed_sub = sum(1 for s in task_subitems if s["status"] == "completed")

    # 计算每个任务的耗时
    for tk in tasks:
        t0 = _parse_ts(tk["start_ts"])
        t1 = _parse_ts(tk["end_ts"])
        tk["duration_s"] = (t1 - t0) if (t0 is not None and t1 is not None) else None

    # ===== 任务完成判定（启发式） =====
    # 判据：has_final_text（输出了最终结论文本）优先视为「已完成」；
    # stop_reason == end_turn 且 has_final_text 更可信；tool_use 结尾且无 text → 疑似中断。
    for tk in tasks:
        has_text = tk.get("has_final_text", False)
        stop = tk.get("stop_reason", "")
        if has_text and stop == "end_turn":
            tk["completion"] = "completed"
            tk["completion_reason"] = "有最终结论文本 + 正常收尾"
        elif has_text:
            tk["completion"] = "completed"
            tk["completion_reason"] = "有最终结论文本"
        elif stop == "end_turn":
            tk["completion"] = "partial"
            tk["completion_reason"] = "正常收尾但无明确文本结论"
        else:
            tk["completion"] = "interrupted"
            tk["completion_reason"] = "以工具调用结尾，未见最终结论"

    # 会话总耗时
    session_duration = None
    t_first = _parse_ts(ts_first)
    t_last = _parse_ts(ts_last)
    if t_first is not None and t_last is not None:
        session_duration = t_last - t_first

    return {
        "session_id": session_id,
        "jsonl_path": str(path),
        "range": {"first": ts_first, "last": ts_last},
        "duration_s": session_duration,
        "model_usage": model_usage,
        "tool_dist": tool_dist,
        "total_tool_calls": total_tool_calls,
        "tokens": tokens,
        "tasks": tasks,
        "task_subitems": task_subitems,
        "skill_events": skill_events,
        "skill_triggered_tasks": skill_triggered_tasks,
        "skill_script_tasks": skill_script_tasks,
        "skill_used_tasks": skill_used_tasks,
        "task_count": len(tasks),
        "blocks": blocks,
        "completed_sub": completed_sub,
        "total_sub": len(task_subitems),
        "human_interventions": human_interventions,
        "total_human_interventions": len(human_interventions),
    }


# ===== HTML 渲染 =====

def render_html(data):
    """把解析结果渲染成自包含 HTML。"""
    # 把数据序列化，注入 textarea（避免特殊字符破坏 JS）
    data_json = json.dumps(data, ensure_ascii=False)

    # 预计算一些渲染数值
    fmt_tok = _fmt_tokens
    fmt_dur = _fmt_duration

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>单会话评测报告 · {data['session_id'][:8]}</title>
<style>
  :root {{
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: {SURFACE};
    color: {INK_PRIMARY};
    line-height: 1.6;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }}

  .hero {{
    background: {CARD};
    border: 1px solid {GRID};
    border-radius: 12px;
    padding: 24px 28px;
    margin-bottom: 24px;
  }}
  .hero h1 {{
    margin: 0 0 4px;
    font-size: 22px;
    font-weight: 700;
  }}
  .hero .sub {{
    color: {INK_MUTED};
    font-size: 13px;
    margin-bottom: 16px;
    word-break: break-all;
  }}
  .hero-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 16px;
  }}
  .hitem {{ text-align: center; }}
  .hitem .v {{ font-size: 26px; font-weight: 700; color: {ACCENT}; }}
  .hitem .k {{ color: {INK_MUTED}; font-size: 12px; }}

  .panel {{
    background: {CARD};
    border: 1px solid {GRID};
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 20px;
  }}
  .panel h2 {{
    margin: 0 0 16px;
    font-size: 16px;
    font-weight: 700;
    color: {INK_PRIMARY};
  }}

  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }}
  .stat {{
    background: {SURFACE};
    border: 1px solid {GRID};
    border-radius: 8px;
    padding: 14px 16px;
  }}
  .stat .label {{ color: {INK_MUTED}; font-size: 12px; }}
  .stat .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}

  .task-card {{
    border: 1px solid {GRID};
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
    background: {SURFACE};
  }}
  .task-card .q {{ font-size: 15px; font-weight: 600; margin-bottom: 4px; }}
  .task-card .meta {{ color: {INK_SECONDARY}; font-size: 12px; margin-bottom: 10px; }}
  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 6px;
  }}
  .badge-skill {{ background: rgba(45,164,78,0.15); color: #3fb950; }}
  .badge-noskill {{ background: rgba(110,118,129,0.18); color: {INK_MUTED}; }}
  .badge-blocked {{ color: {RED}; font-weight: 700; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid {GRID}; }}
  th {{ color: {INK_MUTED}; font-weight: 600; font-size: 12px; }}
  td.num, th.num {{ text-align: right; font-family: ui-monospace, monospace; }}
  .status-icon {{ font-size: 14px; }}

  .bar {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }}
  .bar .name {{ width: 220px; color: {INK_SECONDARY}; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar .track {{ flex: 1; background: {SURFACE}; border-radius: 4px; height: 12px; overflow: hidden; }}
  .bar .fill {{ height: 100%; background: {ACCENT}; border-radius: 4px; }}
  .bar .cnt {{ width: 70px; text-align: right; font-family: ui-monospace, monospace; font-size: 12px; color: {INK_PRIMARY}; }}

  .muted {{ color: {INK_MUTED}; }}
  .empty {{ color: {INK_MUTED}; font-style: italic; }}
  .mono {{ font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
<div class="wrap" id="root">加载中…</div>
<textarea id="data-store" style="display:none;">{data_json}</textarea>
<script>
(function() {{
  const data = JSON.parse(document.getElementById('data-store').value);
  const root = document.getElementById('root');

  function esc(s) {{
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[c]));
  }}
  function fmtTok(n) {{
    if (n >= 100000000) return (n/100000000).toFixed(2) + ' 亿';
    if (n >= 10000) return (n/10000).toFixed(1) + ' 万';
    return n.toLocaleString();
  }}
  function fmtDur(s) {{
    if (s === null || s === undefined) return '-';
    if (s < 60) return s.toFixed(1) + ' 秒';
    if (s < 3600) return Math.floor(s/60) + ' 分 ' + Math.floor(s%60) + ' 秒';
    return Math.floor(s/3600) + ' 小时 ' + Math.floor((s%3600)/60) + ' 分';
  }}
  function fmtTime(iso) {{
    if (!iso) return '-';
    return iso.replace('T', ' ').replace('Z', '').slice(0, 19);
  }}
  const STATUS_ICON = {{
    'completed': '✅', 'in_progress': '🔄', 'blocked': '⛔', 'pending': '⏳'
  }};
  const STATUS_LABEL = {{
    'completed': '已完成', 'in_progress': '进行中', 'blocked': '阻塞', 'pending': '待处理'
  }};
  function statusCell(s) {{
    const icon = STATUS_ICON[s.status] || '';
    const label = STATUS_LABEL[s.status] || s.status;
    const cls = s.status === 'blocked' ? ' class="badge-blocked"' : '';
    return '<span' + cls + '>' + icon + ' ' + label + '</span>';
  }}

  let h = '';

  // ===== Hero =====
  const tok = data.tokens;
  h += '<div class="hero">';
  h += '<h1>单会话评测报告</h1>';
  h += '<div class="sub mono">' + esc(data.session_id) + '</div>';
  h += '<div class="hero-grid">';
  h += '<div class="hitem"><div class="v">' + data.task_count + '</div><div class="k">真实任务数</div></div>';
  h += '<div class="hitem"><div class="v">' + fmtDur(data.duration_s) + '</div><div class="k">会话总耗时</div></div>';
  h += '<div class="hitem"><div class="v">' + (data.task_count ? (100*data.skill_used_tasks/data.task_count).toFixed(0) + '%' : '-') + '</div><div class="k">Skill 使用率（含脚本直用）</div></div>';
  h += '<div class="hitem"><div class="v">' + fmtTok(tok.input + tok.cache_read + tok.output) + '</div><div class="k">总 Token</div></div>';
  h += '<div class="hitem"><div class="v">' + data.total_tool_calls + '</div><div class="k">工具调用总数</div></div>';
  h += '<div class="hitem"><div class="v">' + (data.total_human_interventions || 0) + '</div><div class="k">人工介入次数</div></div>';
  h += '</div>';
  h += '</div>';

  // ===== 总指标 =====
  h += '<div class="panel"><h2>总指标</h2>';
  h += '<div class="stat-grid">';
  h += '<div class="stat"><div class="label">Input</div><div class="value">' + fmtTok(tok.input) + '</div></div>';
  h += '<div class="stat"><div class="label">Cache Read</div><div class="value">' + fmtTok(tok.cache_read) + '</div></div>';
  h += '<div class="stat"><div class="label">Cache Write</div><div class="value">' + fmtTok(tok.cache_write) + '</div></div>';
  h += '<div class="stat"><div class="label">Output</div><div class="value">' + fmtTok(tok.output) + '</div></div>';
  h += '<div class="stat"><div class="label">任务完成度</div><div class="value">' + data.completed_sub + ' / ' + data.total_sub + '</div></div>';
  h += '</div>';

  const cacheTotal = tok.input + tok.cache_read;
  const cacheHit = cacheTotal ? (100 * tok.cache_read / cacheTotal).toFixed(1) : 0;
  h += '<div class="muted" style="margin-top:14px;font-size:12px">Cache 命中率：' + cacheHit + '%</div>';
  h += '</div>';

  // ===== Skill 触发（两种口径） =====
  h += '<div class="panel"><h2>Skill 触发（两种口径）</h2>';
  h += '<div class="stat-grid">';
  const rExplicit = data.task_count ? (100*data.skill_triggered_tasks/data.task_count).toFixed(0) + '%' : '-';
  const rUsed = data.task_count ? (100*data.skill_used_tasks/data.task_count).toFixed(0) + '%' : '-';
  h += '<div class="stat"><div class="label">口径 A · 显式 Skill 工具</div><div class="value">' + rExplicit + '</div><div class="label" style="margin-top:4px">' + data.skill_triggered_tasks + ' / ' + data.task_count + ' 个任务</div></div>';
  h += '<div class="stat"><div class="label">口径 B · 含脚本直用</div><div class="value">' + rUsed + '</div><div class="label" style="margin-top:4px">' + data.skill_used_tasks + ' / ' + data.task_count + ' 个任务</div></div>';
  h += '</div>';
  h += '<div class="muted" style="margin-top:14px;font-size:12px">口径 B 额外识别了「绕开 Skill 工具、直接 Bash/Read 执行 skills/&lt;名&gt;/ 目录下脚本」的使用方式。两者并存，因为 agent 可能在预加载后不再显式调用 Skill 工具。</div>';
  h += '</div>';

  // ===== 工具分布 =====
  h += '<div class="panel"><h2>工具调用分布</h2>';
  const toolEntries = Object.entries(data.tool_dist).sort((a,b) => b[1]-a[1]);
  const maxTool = toolEntries.length ? toolEntries[0][1] : 1;
  toolEntries.forEach(([name, cnt]) => {{
    h += '<div class="bar">';
    h += '<div class="name" title="' + esc(name) + '">' + esc(name) + '</div>';
    h += '<div class="track"><div class="fill" style="width:' + (100*cnt/maxTool).toFixed(1) + '%"></div></div>';
    h += '<div class="cnt">' + cnt + '</div>';
    h += '</div>';
  }});
  h += '</div>';

  // ===== 模型 =====
  h += '<div class="panel"><h2>模型使用</h2>';
  const mEntries = Object.entries(data.model_usage);
  mEntries.forEach(([m, cnt]) => {{
    h += '<div class="bar">';
    h += '<div class="name">' + esc(m) + '</div>';
    h += '<div class="track"><div class="fill" style="width:100%"></div></div>';
    h += '<div class="cnt">' + cnt + ' 轮</div>';
    h += '</div>';
  }});
  h += '</div>';

  // ===== Skill 触发时间线 =====
  h += '<div class="panel"><h2>Skill 触发时间线</h2>';
  if (data.skill_events.length === 0) {{
    h += '<div class="empty">本会话未触发任何 Skill</div>';
  }} else {{
    h += '<table><thead><tr><th>时间</th><th>Skill</th></tr></thead><tbody>';
    data.skill_events.forEach(e => {{
      h += '<tr><td class="mono">' + fmtTime(e.ts) + '</td><td>⚡ ' + esc(e.skill_name) + '</td></tr>';
    }});
    h += '</tbody></table>';
    if (data.task_count > 0) {{
      h += '<div class="muted" style="margin-top:10px;font-size:12px">注意：Skill 工具调用可能发生在用户指令之前（agent 预加载），此时其时间戳会归属于较早的那个任务，不代表该任务实际上依赖此 Skill。</div>';
    }}
  }}
  h += '</div>';

  // ===== 人工介入 =====
  h += '<div class="panel"><h2>人工介入（AskUserQuestion）</h2>';
  if (!data.human_interventions || data.human_interventions.length === 0) {{
    h += '<div class="empty">本会话 agent 未反向追问用户</div>';
  }} else {{
    h += '<div class="muted" style="font-size:12px;margin-bottom:10px">共 ' + data.total_human_interventions + ' 次——agent 中途停下向用户澄清，属「人工介入」信号。</div>';
    data.human_interventions.forEach(q => {{
      h += '<div class="task-card" style="margin-bottom:10px">';
      h += '<div class="q">🙋 ' + esc(q.question) + '</div>';
      h += '<div class="meta">' + fmtTime(q.ts) + (q.header ? ' · ' + esc(q.header) : '') + '</div>';
      if (q.options && q.options.length) {{
        h += '<div class="muted" style="font-size:12px">选项：' + q.options.map(o => esc(o)).join(' / ') + '</div>';
      }}
      h += '</div>';
    }});
  }}
  h += '</div>';

  // ===== 阻塞清单 =====
  h += '<div class="panel"><h2>阻塞清单</h2>';
  if (data.blocks.length === 0) {{
    h += '<div class="empty">未检测到阻塞（全程无 blocked 状态）</div>';
  }} else {{
    h += '<table><thead><tr><th>子任务</th><th>状态</th><th>归属任务</th></tr></thead><tbody>';
    data.blocks.forEach(b => {{
      h += '<tr><td>' + esc(b.subject) + '</td><td class="badge-blocked">⛔ 阻塞</td><td class="muted">' + esc(b.belongs_to || '-') + '</td></tr>';
    }});
    h += '</tbody></table>';
  }}
  h += '</div>';

  // ===== 任务列表（按用户对话） =====
  h += '<div class="panel"><h2>任务列表（按用户对话）</h2>';
  data.tasks.forEach((tk, i) => {{
    h += '<div class="task-card">';
    h += '<div class="q">#' + (i+1) + ' ' + esc(tk.query) + '</div>';
    h += '<div class="meta">' + fmtTime(tk.start_ts) + ' → ' + fmtTime(tk.end_ts) + ' · 耗时 ' + fmtDur(tk.duration_s) + '</div>';
    h += '<div class="meta">';
    if (tk.skill_loaded) {{
      h += '<span class="badge badge-skill">⚡ 显式加载 ' + esc(tk.skill_loaded) + '</span>';
    }}
    if (tk.skill_via_script) {{
      h += '<span class="badge badge-skill">📜 脚本直用 ' + esc(tk.skill_via_script) + '</span>';
    }}
    if (!tk.skill_loaded && !tk.skill_via_script) {{
      h += '<span class="badge badge-noskill">未使用 Skill</span>';
    }}
    h += '<span class="badge badge-noskill">工具 ' + tk.tool_calls + ' 次</span>';
    h += '<span class="badge badge-noskill">Input ' + fmtTok(tk.tokens.input) + '</span>';
    h += '<span class="badge badge-noskill">Output ' + fmtTok(tk.tokens.output) + '</span>';
    h += '</div>';

    // 完成状态
    const COMP_LABEL = {{'completed': '已完成', 'partial': '部分完成', 'interrupted': '疑似中断'}};
    const COMP_CLASS = {{'completed': 'badge-skill', 'partial': 'badge-noskill', 'interrupted': 'badge-blocked'}};
    const comp = tk.completion || 'interrupted';
    h += '<div class="meta" style="margin-bottom:6px">';
    h += '<span class="badge ' + (COMP_CLASS[comp]||'badge-noskill') + '">' + (comp==='completed'?'✓':comp==='partial'?'◐':'✗') + ' ' + (COMP_LABEL[comp]||comp) + '</span>';
    if (tk.completion_reason) h += '<span class="muted" style="font-size:11px">' + esc(tk.completion_reason) + '</span>';
    if (tk.human_interventions > 0) h += '<span class="badge badge-blocked">🙋 人工介入 ' + tk.human_interventions + ' 次</span>';
    h += '</div>';

    // 该任务的嵌套子任务
    const subs = data.task_subitems.filter(s => s.belongs_to === tk.query);
    if (subs.length) {{
      h += '<table style="margin-top:8px"><thead><tr><th>子任务</th><th class="num">状态</th><th class="num">耗时</th></tr></thead><tbody>';
      subs.forEach(s => {{
        h += '<tr><td>' + esc(s.subject) + '</td><td class="num status-icon">' + statusCell(s) + '</td><td class="num">' + fmtDur(s.duration_s) + '</td></tr>';
      }});
      h += '</tbody></table>';
    }}
    h += '</div>';
  }});
  h += '</div>';

  // ===== TaskList 子任务执行追踪 =====
  h += '<div class="panel"><h2>TaskList 子任务执行追踪</h2>';
  if (data.task_subitems.length === 0) {{
    h += '<div class="empty">该会话未创建 TaskList（无 TaskCreate/TaskUpdate）</div>';
  }} else {{
    h += '<table><thead><tr><th>子任务</th><th class="num">状态</th><th class="num">耗时</th><th>归属任务</th></tr></thead><tbody>';
    data.task_subitems.forEach(s => {{
      h += '<tr>';
      h += '<td>' + esc(s.subject) + '</td>';
      h += '<td class="num status-icon">' + statusCell(s) + '</td>';
      h += '<td class="num">' + fmtDur(s.duration_s) + '</td>';
      h += '<td class="muted">' + esc(s.belongs_to || '-') + '</td>';
      h += '</tr>';
    }});
    h += '</tbody></table>';
  }}
  h += '</div>';

  root.innerHTML = h;
}})();
</script>
</body>
</html>"""

    return html


def main():
    ap = argparse.ArgumentParser(description="单会话深度评测 HTML 报告")
    ap.add_argument(
        "--jsonl",
        default=r"C:\Users\wangjing71\.claude\projects\D--wy-projects-work-4-log\625d0eda-7662-42c8-9091-49603b17e203.jsonl",
        help="单个 .jsonl 会话日志路径",
    )
    ap.add_argument("--out", default=None, help="输出 HTML 路径，缺省 results/session_report.html")
    args = ap.parse_args()

    data = scan_single_session(args.jsonl)

    # 终端摘要
    print("=== 单会话评测摘要 ===")
    print(f"session_id: {data['session_id']}")
    print(f"真实任务数: {data['task_count']} | Skill 触发任务: {data['skill_triggered_tasks']}")
    print(f"工具调用: {data['total_tool_calls']} | token: input {data['tokens']['input']:,} / cache_read {data['tokens']['cache_read']:,} / output {data['tokens']['output']:,}")
    print(f"子任务: {data['completed_sub']}/{data['total_sub']} 完成 | 阻塞: {len(data['blocks'])}")
    print()

    out = args.out or str(Path(__file__).parent / "results" / "session_report.html")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(data), encoding="utf-8")
    print(f"报告已生成: {out_path}")


if __name__ == "__main__":
    main()
