# 评测模型提供商配置（provider）

evalkit 评测驱动 `claude --print --output-format stream-json` 子进程。默认情况下 claude CLI
继承它自己的 `~/.claude/settings.json`（`env` + `hooks`），例如本机经 codemaker 本地代理
（`http://127.0.0.1:15721`）接入 `auto_deepseek_plan[1m]`。

本功能让评测**显式指定**模型提供商（参考 Claude Code settings.json 的 env + hooks 结构），
不修改、不污染你的系统配置——通过 `claude --settings '<JSON>'` 附加加载，评测结束即失效。

## 配置位置

`D:\wy_projects\evalkit\conf.json` 的 `provider` 段（或独立 `providers.json`，两者并存时
providers.json 优先）。

```jsonc
{
  "provider": {
    // 便捷字段写法：base_url / token / api_key / model 自动归一为标准环境变量
    "codemaker_deepseek": {
      "base_url": "http://127.0.0.1:15721",
      "token": "codemaker-managed",
      "model": "auto_deepseek_plan[1m]",
      // 标准写法：直接给 env（仅在白名单内的变量生效）
      // "env": { "ANTHROPIC_BASE_URL": "...", "ANTHROPIC_AUTH_TOKEN": "...", "ANTHROPIC_MODEL": "..." },
      // hooks 与 claude settings.json 同构（仅合法事件生效）
      "hooks": {
        "PreToolUse": [
          { "matcher": "Bash", "hooks": [{ "type": "http", "url": "http://127.0.0.1:15721/hook/claude/pre-tool" }] }
        ],
        "UserPromptSubmit": [
          { "matcher": "*", "hooks": [{ "type": "http", "url": "http://127.0.0.1:15721/hook/claude/user-prompt" }] }
        ]
      }
    },
    // default：空 env/hooks = 不注入，沿用 claude 系统配置（推荐默认）
    "default": { "env": {}, "hooks": {} }
  }
}
```

支持的事件（与 Claude Code 一致）：`PreToolUse` `PostToolUse` `PostToolUseFailure`
`UserPromptSubmit` `Notification` `Stop` `StopFailure` `SubagentStart` `SubagentStop`
`SessionStart` `SessionEnd` `PreCompact` `PostCompact`。

env 白名单：`ANTHROPIC_BASE_URL` `ANTHROPIC_AUTH_TOKEN` `ANTHROPIC_API_KEY`
`ANTHROPIC_MODEL` `ANTHROPIC_DEFAULT_HAIKU/SONNET/OPUS_MODEL` `ANTHROPIC_SMALL_FAST_MODEL`
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`。

## 使用

- 批量评测：`python eval_batch.py run --tasks-dir tasks/gen --provider codemaker_deepseek ...`
- 看板发起评测：`/api/start` 参数加 `"provider": "codemaker_deepseek"`（可选）
- 列出可用 provider：`python provider.py`

## 行为说明

- 传了 provider 且含 env/hooks → 自动追加 `--settings <JSON>`；含 hooks 时自动加
  `--include-hook-events`（让 hook 生命周期事件进入轨迹）。
- provider 环境变量同时注入子进程 env（覆盖系统值）。
- 未配置 / `default`（空配置）/ 名称不存在 → 完全维持现状（继承 claude 系统配置）。
- 代码：`provider.py`（解析+合成）、`claude_backend.py`（接线）、`eval_batch.py` / `eval_server.py`（透传）。
