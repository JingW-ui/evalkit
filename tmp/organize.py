#!/usr/bin/env python3
"""把根目录的 tmp_*.py 一次性脚本移入 tmp/ 目录，并修正内部路径（根目录 → 上一级）。"""
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).parent.parent
TMP_DIR = ROOT / "tmp"
TMP_DIR.mkdir(exist_ok=True)

moved = 0
for f in sorted(ROOT.glob("tmp_*.py")):
    new_name = f.name[4:]          # 去掉 tmp_ 前缀
    dst = TMP_DIR / new_name
    txt = f.read_text(encoding="utf-8")
    # 路径修正：脚本从根目录移到 tmp/ 后，项目根 = 上一级
    before = txt
    txt = txt.replace("Path(__file__).resolve().parent.parent", "Path(__file__).resolve().parent.parent.parent")
    txt = txt.replace("Path(__file__).parent.parent", "Path(__file__).parent.parent.parent")
    dst.write_text(txt, encoding="utf-8")
    f.unlink()
    moved += 1
    changed = "（修正路径）" if txt != before else ""
    print(f"[move] {f.name} -> tmp/{new_name} {changed}")

print(f"\n移动 {moved} 个脚本到 tmp/")
