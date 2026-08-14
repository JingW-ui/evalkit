#!/usr/bin/env python3
"""
report_interactive.py — 交互式 HTML 评测报告（单页 SPA 四级下钻）。

下钻结构：总览 → skill → 会话 → 轮次

数据整合：
  - scanner.scan_all() 提供跨会话聚合 + 逐轮 trace（含 prompt/雪球点）
  - cost.compute_cost() 提供每个会话的成本换算
  - analyze.extract_env_fingerprint() 提供每个会话的环境指纹

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
from parser import parse_session_jsonl

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


def fmt_tokens(n):
    if n >= 100_000_000:
        return f"{n/100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n/10_000:.1f} 万"
    return f"{n:,}"


def build_data(proj_dir: str) -> dict:
    """整合 scanner + cost + fingerprint，生成下钻所需的数据树。"""
    scan = scan_all(proj_dir)
    summary = scan["summary"]
    sessions = scan["sessions"]

    enriched = []
    for s in sessions:
        m = {
            "input_tokens": s["total_input"],
            "cache_read_tokens": s["total_cache_read"],
            "cache_write_tokens": 0,
            "output_tokens": s["total_output"],
        }
        cost = compute_cost(m)

        try:
            parsed = parse_session_jsonl(s["jsonl_path"])
            fp = extract_env_fingerprint(s["jsonl_path"], parsed)
            tool_dist = {}
            for t in parsed.get("tool_sequence", []):
                name = t["name"]
                tool_dist[name] = tool_dist.get(name, 0) + 1
        except Exception:
            fp = {"model": s["model"], "skill_count": 0, "mcp_tools_used": 0, "distinct_tools_used": 0}
            tool_dist = {}

        enriched.append({
            "session_id": s["session_id"],
            "total_tokens": s["total_tokens"],
            "model": s["model"],
            "skill_loaded": s["skill_loaded"],
            "trace_count": s["trace_count"],
            "snowball_count": s["snowball_count"],
            "cost": cost,
            "fingerprint": fp,
            "tool_dist": tool_dist,
            "traces": s["traces"],
            "anomalies": s["anomalies"],
        })

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


def render_html(data: dict) -> str:
    """渲染单文件 HTML，内嵌数据 + JS 交互。

    数据放在 <textarea id="data-store"> 中，JS 运行时 JSON.parse，
    避免直接 const DATA = {...} 时 JSON 里的特殊字符破坏 JS 语法。
    """
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    # 把 JS 模板存为纯字符串（不用 f-string 的 {{ }} 转义）
    js_template = r"""
(function() {
  let DATA, COLORS;
  try {
    DATA = JSON.parse(document.getElementById('data-store').value);
    COLORS = ["#5b8ff9","#d95926","#199e70","#c98500","#d55181","#9085e9","#e66767","#6dc8ec"];
  } catch(e) {
    document.getElementById('error-display').style.display = 'block';
    document.getElementById('error-display').textContent = 'DATA parse error: ' + e.message;
    return;
  }
  window.DATA = DATA;
  window.COLORS = COLORS;

  function fmtT(n) {
    if (n >= 100000000) return (n/100000000).toFixed(2) + ' 亿';
    if (n >= 10000) return (n/10000).toFixed(1) + ' 万';
    return n.toLocaleString();
  }

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function showView(name) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById('view-' + name).classList.add('active');
  }

  window.renderOverview = function() {
    const s = DATA.summary;
    const el = document.getElementById('view-overview');
    let html = '<div class="stats">' +
      '<div class="stat"><div class="stat-label">会话总数</div><div class="stat-value">' + s.session_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value" style="color:#D9382B">' + fmtT(s.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">雪球会话</div><div class="stat-value">' + s.snowball_sessions + '</div></div>' +
      '<div class="stat"><div class="stat-label">平均 Token/会话</div><div class="stat-value">' + fmtT(s.avg_tokens) + '</div></div>' +
    '</div>';

    html += '<div class="panel"><h3>按 Skill 分布（点击下钻）</h3>';
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

    html += '<div class="panel"><h3>Top 消耗会话</h3><table><thead>' +
      '<tr><th>Session</th><th>Token</th><th>模型</th><th>Skill</th><th>雪球</th></tr></thead><tbody>';
    DATA.sessions.slice(0, 15).forEach((sess) => {
      html += '<tr>' +
        '<td><code>' + esc(sess.session_id.slice(0,8)) + '</code></td>' +
        '<td class="num">' + fmtT(sess.total_tokens) + '</td>' +
        '<td>' + esc(sess.model) + '</td>' +
        '<td>' + esc(sess.skill_loaded || '纯agent') + '</td>' +
        '<td class="num"><span class="' + (sess.snowball_count > 0 ? 'snowball-dot' : 'muted') + '">' + sess.snowball_count + '</span></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';

    el.innerHTML = html;
  };

  window.renderSkill = function(idx) {
    const sk = DATA.skills[idx];
    const el = document.getElementById('view-skill');
    let html = '<button class="back-btn" onclick="showView(\'overview\');renderOverview()">← 返回总览</button>';
    html += '<h3>' + esc(sk.skill) + '</h3>';
    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">会话数</div><div class="stat-value">' + sk.session_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value" style="color:#D9382B">' + fmtT(sk.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">雪球点</div><div class="stat-value">' + sk.snowball_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">平均/会话</div><div class="stat-value">' + fmtT(Math.round(sk.total_tokens / sk.session_count)) + '</div></div>' +
    '</div>';

    html += '<div class="panel"><table><thead><tr><th>Session</th><th>Token</th><th>成本</th><th>模型</th><th>工具数</th><th>雪球</th></tr></thead><tbody>';
    sk.sessions.forEach((sess, i) => {
      html += '<tr class="clickable" onclick="renderSession(' + idx + ',' + i + ')">' +
        '<td><code>' + esc(sess.session_id.slice(0,8)) + '</code></td>' +
        '<td class="num">' + fmtT(sess.total_tokens) + '</td>' +
        '<td class="num">¥' + sess.cost.total_cost.toFixed(2) + '</td>' +
        '<td>' + esc(sess.model) + '</td>' +
        '<td class="num">' + Object.keys(sess.tool_dist).length + '</td>' +
        '<td class="num"><span class="' + (sess.snowball_count > 0 ? 'snowball-dot' : 'muted') + '">' + sess.snowball_count + '</span></td>' +
      '</tr>';
    });
    html += '</tbody></table></div>';
    el.innerHTML = html;
    showView('skill');
  };

  window.renderSession = function(skillIdx, sessIdx) {
    const sess = DATA.skills[skillIdx].sessions[sessIdx];
    const el = document.getElementById('view-session');
    let html = '<button class="back-btn" onclick="renderSkill(' + skillIdx + ')">← 返回 ' + esc(DATA.skills[skillIdx].skill) + '</button>';
    html += '<h3><code>' + esc(sess.session_id) + '</code></h3>';

    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">总 Token</div><div class="stat-value">' + fmtT(sess.total_tokens) + '</div></div>' +
      '<div class="stat"><div class="stat-label">成本</div><div class="stat-value" style="color:#D9382B">¥' + sess.cost.total_cost.toFixed(2) + '</div></div>' +
      '<div class="stat"><div class="stat-label">Trace 数</div><div class="stat-value">' + sess.trace_count + '</div></div>' +
      '<div class="stat"><div class="stat-label">雪球点</div><div class="stat-value">' + sess.snowball_count + '</div></div>' +
    '</div>';

    html += '<div class="panel"><h3>成本分项</h3><table>' +
      '<tr><td>输入</td><td class="num">¥' + sess.cost.input_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>缓存读</td><td class="num">¥' + sess.cost.cache_read_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>缓存写</td><td class="num">¥' + sess.cost.cache_write_cost.toFixed(2) + '</td></tr>' +
      '<tr><td>输出</td><td class="num">¥' + sess.cost.output_cost.toFixed(2) + '</td></tr>' +
    '</table></div>';

    html += '<div class="panel"><h3>环境指纹</h3>';
    html += '<span class="muted">模型 ' + esc(sess.fingerprint.model) + ' · skill数 ' + (sess.fingerprint.skill_count || 0) +
            ' · mcp工具数 ' + (sess.fingerprint.mcp_tools_used || 0) + ' · 工具种类 ' + (sess.fingerprint.distinct_tools_used || 0) + '</span></div>';

    const tools = Object.entries(sess.tool_dist).sort((a,b) => b[1]-a[1]);
    html += '<div class="panel"><h3>工具调用分布</h3><table><thead><tr><th>工具</th><th>次数</th></tr></thead><tbody>';
    tools.slice(0, 20).forEach(([name, cnt]) => {
      html += '<tr><td><code>' + esc(name) + '</code></td><td class="num">' + cnt + '</td></tr>';
    });
    html += '</tbody></table></div>';

    if (sess.traces && sess.traces.length) {
      html += '<div class="panel"><h3>逐轮 Token 趋势（点击红点看轮次详情）</h3>';
      html += window.trendSVG(sess.traces, skillIdx, sessIdx);
      html += '</div>';
    }

    el.innerHTML = html;
    showView('session');
  };

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

  window.renderTurn = function(skillIdx, sessIdx, roundNum) {
    const sess = DATA.skills[skillIdx].sessions[sessIdx];
    const tr = sess.traces.find(t => t.round === roundNum);
    if (!tr) {
      document.getElementById('error-display').style.display = 'block';
      document.getElementById('error-display').textContent = '未找到第 ' + roundNum + ' 轮';
      return;
    }
    const el = document.getElementById('view-turn');
    let html = '<button class="back-btn" onclick="renderSession(' + skillIdx + ',' + sessIdx + ')">← 返回会话</button>';
    html += '<h3>第 ' + tr.round + ' 轮详情</h3>';
    html += '<div class="stats">' +
      '<div class="stat"><div class="stat-label">该轮 Token</div><div class="stat-value">' + fmtT(tr.round_tokens || 0) + '</div></div>' +
      '<div class="stat"><div class="stat-label">增量</div><div class="stat-value" style="color:' + (tr.is_snowball ? '#D9382B' : '#e8e8f0') + '">' + fmtT(tr.increment || 0) + '</div></div>' +
      '<div class="stat"><div class="stat-label">是否雪球</div><div class="stat-value">' + (tr.is_snowball ? ' 是' : '否') + '</div></div>' +
      '<div class="stat"><div class="stat-label">模型</div><div class="stat-value" style="font-size:14px">' + esc(tr.model) + '</div></div>' +
    '</div>';
    if (tr.prompt) {
      html += '<div class="panel"><h3>触发该轮的 Prompt</h3><div class="prompt-box">' + esc(tr.prompt) + '</div></div>';
    }
    el.innerHTML = html;
    showView('turn');
  };

  window.onerror = function(msg, src, line, col, err) {
    const el = document.getElementById('error-display');
    if (el) {
      el.style.display = 'block';
      el.textContent = '❌ JS错误: ' + msg + '\n at ' + src + ':' + line + ':' + col;
    }
  };

  try {
    renderOverview();
  } catch(e) {
    const el = document.getElementById('error-display');
    el.style.display = 'block';
    el.textContent = '❌ renderOverview 失败: ' + e.message + '\n' + (e.stack || '');
  }
})();
"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>evalkit 交互式评测报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    background-color: {SURFACE}; color: {INK_PRIMARY};
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.6; margin: 0; padding: 24px 20px;
  }}
  .container {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }}
  h1::before {{ content: ""; width: 6px; height: 22px; background: {BRAND_RED}; border-radius: 2px; }}
  h2 {{ font-size: 18px; color: #fff; margin: 24px 0 14px; font-weight: 600; }}
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
  .back-btn {{
    display: inline-block; background: rgba(91,143,249,0.12); border: 1px solid rgba(91,143,249,0.4);
    color: #5b8ff9; border-radius: 6px; padding: 4px 14px; cursor: pointer; font-size: 13px; margin: 0 0 14px;
    transition: background 0.2s;
  }}
  .back-btn:hover {{ background: rgba(91,143,249,0.22); }}
  .bar-row {{ display: flex; align-items: center; gap: 10px; margin: 6px 0; cursor: pointer; }}
  .bar-row:hover {{ opacity: 0.85; }}
  .bar-label {{ width: 140px; font-size: 12px; color: {INK_SECONDARY}; text-align: right; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar-track {{ flex: 1; height: 22px; background: #2a2a42; border-radius: 4px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; }}
  .bar-val {{ width: 70px; font-size: 12px; color: {INK_PRIMARY}; font-variant-numeric: tabular-nums; }}
  .snowball-dot {{ color: {BRAND_RED}; font-weight: 600; }}
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

  <!-- 数据区（textarea 安全承载任意字符） -->
  <textarea id="data-store" style="display:none">{data_json}</textarea>

  <!-- 错误显示 -->
  <div id="error-display"></div>

  <!-- 总览视图 -->
  <div class="view active" id="view-overview"></div>

  <!-- Skill 视图 -->
  <div class="view" id="view-skill"></div>

  <!-- 会话视图 -->
  <div class="view" id="view-session"></div>

  <!-- 轮次视图 -->
  <div class="view" id="view-turn"></div>
</div>

<script>
{js_template}
</script>
</body>
</html>"""

    return html


def main():
    ap = argparse.ArgumentParser(description="生成交互式 HTML 评测报告")
    ap.add_argument("--dir", required=True, help="projects 目录")
    ap.add_argument("--out", default="results/report_interactive.html", help="输出 HTML 路径")
    args = ap.parse_args()

    print("扫描并整合数据...")
    data = build_data(args.dir)
    print(f"  共 {data['summary']['session_count']} 个会话, {len(data['skills'])} 个 skill")

    print("渲染 HTML...")
    html = render_html(data)

    out = Path(__file__).parent / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"交互式报告已生成: {out}")


if __name__ == "__main__":
    main()
