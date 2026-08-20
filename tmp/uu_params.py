#!/usr/bin/env python3
import glob
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

for f in sorted(glob.glob(str(Path(__file__).resolve().parent.parent / "results" / "batch" / "eval-*uu*" / "session.jsonl"))):
    sid = Path(f).parent.name
    q = ""
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "user/message":
                q = ev.get("data", {}).get("content", [{}])[0].get("text", "")
                break
    print(f"{sid}  =>  {q}")
