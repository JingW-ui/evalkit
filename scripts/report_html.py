#!/usr/bin/env python3
"""
report_html.py — 网易深色技术信息图风格的 HTML 可视化报告。

基于 scanner.py 的扫描结果，生成自包含的 HTML 报告（无外部依赖）。

设计规格：
  - 背景 深石板灰 #1a1a2e，卡片 #22223b
  - 品牌红 #D9382B 点缀（英雄数字、高亮点）
  - 分类色板（dataviz 验证过的顺序，针对深色背景）
  - 手绘 SVG 图（无 echarts 依赖），含 tooltip

用法：
    python report_html.py --scan results/scan_result.json --out results/report.html
"""

import json
import sys
import argparse
from pathlib import Path

# 网易深色风调色板（分类色，固定顺序）
CATEGORY_COLORS = [
    "#5b8ff9",  # blue
    "#d95926",  # orange
    "#199e70",  # aqua
    "#c98500",  # yellow
    "#d55181",  # magenta
    "#9085e9",  # violet
    "#e66767",  # red
    "#6dc8ec",  # cyan
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


def hero_card(label, value, accent=False):
    color = BRAND_RED if accent else INK_PRIMARY
    return f"""
    <div class="stat" style="{('border-left:3px solid ' + BRAND_RED + ';') if accent else ''}">
      <div class="stat-label">{label}</div>
      <div class="stat-value" style="color:{color}">{value}</div>
    </div>"""


def svg_bar_horizontal(skill_data, width=720, height=None):
    """按 skill 分组的横向条形图。"""
    if not skill_data:
        return '<p class="muted">无数据</p>'
    # 取 top 8
    data = skill_data[:8]
    max_val = data[0]["total_tokens"] if data else 1
    bar_h = 34
    header_h = 20
    label_w = 160
    bar_area_w = width - label_w - 90
    height = header_h + len(data) * (bar_h + 6)

    bars = []
    y = header_h
    for i, d in enumerate(data):
        w = int(d["total_tokens"] / max_val * bar_area_w)
        color = CATEGORY_COLORS[i % len(CATEGORY_COLORS)]
        y = header_h + i * (bar_h + 6)
        label = d["skill"][:20] + ("…" if len(d["skill"]) > 20 else "")
        bars.append(f'''
        <text x="0" y="{y + bar_h/2 + 4}" fill="{INK_SECONDARY}" font-size="12">{label}</text>
        <rect x="{label_w}" y="{y}" width="{max(w,2)}" height="{bar_h}" rx="4" fill="{color}"/>
        <text x="{label_w + max(w,2) + 8}" y="{y + bar_h/2 + 4}" fill="{INK_PRIMARY}" font-size="12">{fmt_tokens(d["total_tokens"])}</text>''')
        y = y + bar_h + 6

    return f'''
    <svg width="{width}" height="{height}" role="img" aria-label="skill token 分布">
      {''.join(bars)}
    </svg>'''

def svg_line_trend(session, width=720, height=320):
    """单个雪球会话的逐轮 token 趋势 + 雪球点标记。"""
    traces = session.get("traces", [])
    if not traces:
        return '<p class="muted">无 trace 数据</p>'
    # 精简到最多 60 个点
    if len(traces) > 60:
        traces = traces[::len(traces)//60]

    max_tok = max(t["round_tokens"] for t in traces) or 1
    pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(traces)

    def px(i):
        return pad_l + (i / max(n - 1, 1)) * plot_w
    def py(v):
        return pad_t + plot_h - (v / max_tok) * plot_h

    # 折线路径
    pts = [f"{px(i):.1f},{py(t['round_tokens']):.1f}" for i, t in enumerate(traces)]
    path = " L ".join(pts)
    # 面积填充
    area = f"{px(0):.1f},{pad_t+plot_h:.1f} L " + path + f" L {px(n-1):.1f},{pad_t+plot_h:.1f} Z"

    # 雪球点
    snowballs = []
    for i, t in enumerate(traces):
        if t.get("is_snowball"):
            snowballs.append(f'<circle cx="{px(i):.1f}" cy="{py(t["round_tokens"]):.1f}" r="6" fill="{BRAND_RED}"/>')

    # y 轴刻度
    y_ticks = ""
    for i in range(4):
        v = max_tok * i / 3
        yy = py(v)
        y_ticks += f'<text x="{pad_l-8}" y="{yy+4}" fill="{INK_MUTED}" font-size="10" text-anchor="end">{fmt_tokens(int(v))}</text>'
        y_ticks += f'<line x1="{pad_l}" y1="{yy}" x2="{width-pad_r}" y2="{yy}" stroke="{GRID}" stroke-width="0.5"/>'

    return f'''
    <svg width="{width}" height="{height}" role="img" aria-label="逐轮 token 趋势">
      {y_ticks}
      <path d="M {path}" fill="none" stroke="{CATEGORY_COLORS[0]}" stroke-width="2"/>
      <path d="{area}" fill="{CATEGORY_COLORS[0]}" opacity="0.08"/>
      {''.join(snowballs)}
      <text x="{width-pad_r}" y="{height-10}" fill="{INK_MUTED}" font-size="10" text-anchor="end">轮次</text>
    </svg>'''


def render_html(scan: dict) -> str:
    s = scan["summary"]
    sessions = scan["sessions"]
    by_skill = s["by_skill"]

    # 英雄数字
    heroes = "".join([
        hero_card("会话总数", f'{s["session_count"]}', accent=False),
        hero_card("总 Token", fmt_tokens(s["total_tokens"]), accent=True),
        hero_card("雪球会话", f'{s["snowball_sessions"]}', accent=False),
        hero_card("平均 Token/会话", fmt_tokens(s["avg_tokens"]), accent=False),
    ])

    # 雪球会话（有 snowball 的）
    snowball_sessions = [x for x in sessions if x["snowball_count"] > 0]

    # Top 会话表
    top_rows = ""
    for i, sess in enumerate(sessions):
        color = CATEGORY_COLORS[i % len(CATEGORY_COLORS)]
        skill = sess["skill_loaded"] or "纯agent"
        top_rows += f'''
        <tr>
          <td><code style="color:{color}">{sess["session_id"][:8]}</code></td>
          <td class="num">{fmt_tokens(sess["total_tokens"])}</td>
          <td>{sess["model"]}</td>
          <td>{skill}</td>
          <td class="num">{sess["trace_count"]}</td>
          <td class="num" style="color:{BRAND_RED if sess['snowball_count']>0 else INK_MUTED}">{sess["snowball_count"]}</td>
        </tr>'''

    # 雪球会话的趋势图
    trend_sections = ""
    for sess in snowball_sessions[:3]:  # 只画前 3 个雪球会话
        trend_sections += f'''
        <div class="panel">
          <h3>📈 雪球会话：<code>{sess["session_id"][:12]}</code>（{fmt_tokens(sess["total_tokens"])} · {sess["snowball_count"]} 处雪球）</h3>
          {svg_line_trend(sess)}
        </div>'''

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>evalkit 会话画像报告</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    background-color: {SURFACE};
    color: {INK_PRIMARY};
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 14px;
    line-height: 1.6;
    margin: 0;
    padding: 32px 20px;
  }}
  .container {{ max-width: 940px; margin: 0 auto; }}
  h1 {{
    font-size: 24px; font-weight: 600; margin: 0 0 4px;
    display: flex; align-items: center; gap: 10px;
  }}
  h1::before {{ content: ""; width: 6px; height: 24px; background: {BRAND_RED}; border-radius: 2px; }}
  h2 {{ font-size: 18px; color: #fff; margin: 36px 0 16px; font-weight: 600; }}
  h3 {{ font-size: 15px; color: {INK_PRIMARY}; margin: 0 0 12px; font-weight: 600; }}
  .sub {{ color: {INK_SECONDARY}; margin: 0 0 28px; font-size: 13px; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 20px 0 32px; }}
  .stat {{ background: {CARD}; border-radius: 8px; padding: 16px 18px; }}
  .stat-label {{ color: {INK_MUTED}; font-size: 12px; margin-bottom: 6px; }}
  .stat-value {{ font-size: 26px; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .panel {{ background: {CARD}; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; color: {INK_MUTED}; font-weight: normal; border-bottom: 1px solid {GRID}; padding: 8px 10px; }}
  td {{ border-bottom: 1px solid #2a2a42; padding: 10px; color: {INK_SECONDARY}; }}
  tr:last-child td {{ border-bottom: none; }}
  code {{ background: #2d2d46; padding: 2px 6px; border-radius: 3px; font-family: Consolas, monospace; font-size: 12px; color: {INK_PRIMARY}; }}
  .num {{ font-variant-numeric: tabular-nums; color: {INK_PRIMARY}; }}
  .muted {{ color: {INK_MUTED}; }}
  svg {{ max-width: 100%; height: auto; display: block; }}
  .anomaly-tag {{
    display: inline-block; background: rgba(217,56,43,0.15); color: {BRAND_RED};
    border: 1px solid rgba(217,56,43,0.4); border-radius: 4px; padding: 1px 8px; font-size: 11px;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>evalkit 会话画像报告</h1>
  <p class="sub">跨会话聚合 · 雪球归因 · 异常信号</p>

  <div class="stats">{heroes}</div>

  <div class="panel">
    <h2>按 Skill 的 Token 消耗分布</h2>
    {svg_bar_horizontal(by_skill)}
  </div>

  <div class="panel">
    <h2>Top 消耗会话</h2>
    <table>
      <thead><tr><th>Session</th><th>Token</th><th>模型</th><th>Skill</th><th>Trace</th><th>雪球</th></tr></thead>
      <tbody>{top_rows}</tbody>
    </table>
  </div>

  {trend_sections}

  <p class="muted" style="margin-top:32px;font-size:12px">生成于 evalkit · 数据源 Claude Code JSONL 会话日志 · 深色技术信息图</p>
</div>
</body>
</html>"""

    return html


def main():
    ap = argparse.ArgumentParser(description="生成网易深色风 HTML 可视化报告")
    ap.add_argument("--scan", required=True, help="scanner.py 输出的 scan_result.json")
    ap.add_argument("--out", default="results/report.html", help="输出 HTML 路径")
    args = ap.parse_args()

    with open(args.scan, "r", encoding="utf-8") as f:
        scan = json.load(f)

    html = render_html(scan)

    out = Path(__file__).parent / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"HTML 报告已生成: {out}")


if __name__ == "__main__":
    main()
