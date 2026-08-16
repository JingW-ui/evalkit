# -*- coding: utf-8 -*-
"""单元测试：provider.py（模型提供商配置，参考 Claude Code settings.json env+hooks）。

验证：conf.json provider 段解析、env 便捷字段归一、hooks 过滤、settings JSON 合成、
apply_env 覆盖不污染、claude_backend 接线。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) provider 模块解析 ----
import provider

providers = provider.list_providers()
check("发现 provider（default + codemaker_deepseek）",
      any(p["name"] == "default" for p in providers)
      and any(p["name"] == "codemaker_deepseek" for p in providers),
      f"实际 {[p['name'] for p in providers]}")

p = provider.resolve_provider("codemaker_deepseek")
check("resolve codemaker_deepseek 非空", p is not None)
if p:
    check("env 含 BASE_URL/AUTH_TOKEN/MODEL",
          p["env"].get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:15721"
          and p["env"].get("ANTHROPIC_AUTH_TOKEN") == "codemaker-managed"
          and "auto_deepseek" in p["env"].get("ANTHROPIC_MODEL", ""),
          f"env={p['env']}")

# ---- 2) settings JSON 合成 ----
js = provider.build_settings_json(p)
check("settings JSON 含 env 且为合法 JSON", js and "ANTHROPIC_BASE_URL" in js)
if js:
    parsed = json.loads(js)
    check("settings JSON 可解析且 env 正确",
          parsed.get("env", {}).get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:15721")

# ---- 3) apply_env 覆盖不污染 ----
env = provider.apply_env(p, {"PATH": "x", "ANTHROPIC_BASE_URL": "old"})
check("apply_env 覆盖 BASE_URL", env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:15721")
check("apply_env 保留 PATH", env["PATH"] == "x")

# ---- 4) default（空配置 → 不注入）----
d = provider.resolve_provider("default")
check("default 不注入 settings", provider.build_settings_json(d) is None)

# ---- 5) hooks 过滤：自定义 provider 只留合法事件 ----
custom = provider._normalize_provider("t", {
    "base_url": "http://x", "model": "m",
    "hooks": {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}],
        "NotARealEvent": [{"matcher": "*"}],   # 应被过滤
        "UserPromptSubmit": [{"matcher": "skill*", "hooks": [{"type": "http", "url": "http://h"}]}],
    },
})
check("hooks 只留合法事件",
      "PreToolUse" in custom["hooks"] and "UserPromptSubmit" in custom["hooks"]
      and "NotARealEvent" not in custom["hooks"],
      f"hooks={list(custom['hooks'].keys())}")
check("便捷字段归一 base_url/model",
      custom["env"].get("ANTHROPIC_BASE_URL") == "http://x"
      and custom["env"].get("ANTHROPIC_MODEL") == "m")

# ---- 6) claude_backend 接线 ----
from claude_backend import ClaudeEvalBackend
b = ClaudeEvalBackend(provider="codemaker_deepseek", cwd=".")
check("backend 解析 provider env",
      b.provider and b.provider["env"].get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:15721")
b2 = ClaudeEvalBackend(cwd=".")
check("无配置 → provider None", b2.provider is None)
b3 = ClaudeEvalBackend(provider="nonexistent_xyz", cwd=".")
check("不存在 provider → None 不炸", b3.provider is None)

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(0 if not FAILS else 1)
