#!/usr/bin/env python3
"""
judge_llm.py — LLM 判结论（LLM-as-a-judge）。

判分模型默认 glm-5.2（经 codemaker 网关 127.0.0.1:15721，Anthropic Messages API），
temperature=0 + 强制 JSON。三输入：
  1. 任务题目 query（值的唯一来源）
  2. 预期结果 expected_answer.result/.process
  3. 参考结论 references.json 的 content（仅作完整度/格式参照，值不参与比对）
  4. 被测最终结论 final_text（被测 agent 最后一条 assistant 文本消息）
返回 {"pass": bool, "reason": str}；网关异常时 raise，由上层兜底。

用法：
    from judge_llm import judge
    r = judge(query, expected_answer, reference, final_text)
    print(r["pass"], r["reason"])
"""

import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

_DEFAULT_BASE_URL = "http://127.0.0.1:15721"
_DEFAULT_TOKEN = "codemaker-managed"
_DEFAULT_MODEL = "glm-5.2"
_DEFAULT_MAX_TOKENS = 4096

# 运行期覆盖（由批次 API / 前端下发），优先级：函数参数 > 覆盖 > conf.json
_override = {"model": None, "max_tokens": None}


def set_judge_config(model: str = None, max_tokens: int = None) -> None:
    """设置判分模型/参数的运行期默认（供 start_batch 根据前端选择下发）。"""
    _override["model"] = model or None
    _override["max_tokens"] = max_tokens or None


def _load_conf() -> dict:
    """读 conf.json，取 judge 段 + provider 段（base_url/token/model）。"""
    try:
        conf = json.loads((Path(__file__).parent / "conf.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    judge = conf.get("judge") or {}
    provider_name = judge.get("provider") or "codemaker_deepseek"
    prov = (conf.get("provider") or {}).get(provider_name) or {}
    return {
        "model": judge.get("model") or _DEFAULT_MODEL,
        "base_url": prov.get("base_url") or _DEFAULT_BASE_URL,
        "token": prov.get("token") or _DEFAULT_TOKEN,
    }


_PROMPT = '''你是评测裁判，判断被测 agent 是否【正确完成并报告】了任务。

【任务题目】（值的唯一来源，所有参数以这里为准）
{query}

【预期结果】（最终结论必须报告的内容）
{expected}

【参考结论】（一份正确完成该任务的历史样例，用于对照"完整度/格式"。稳定设备属性——主机名/系统版本/用户/serialno——的值可作事实基准；易变项——分辨率/软件版本号/设备台数/设备ID/验证码——可能随环境变化（虚拟屏接管、软件自升级、设备池增减），如实报告当前值即可，不与参考硬比对；任务参数如设备串号/验证码/文件路径/URL 的值以【任务题目】为准）
```
{reference}
```

【被测结论】（仅取被测 agent 最终一条 assistant 文本消息）
```
{actual}
```

只输出一个 JSON 对象，不要任何其它文字或 markdown：
{{"pass": true, "reason": "≤40字理由"}}

判据：
1. 被测结论是否报告了【预期结果】要求的全部关键项（设备ID / 验证码 / 主机名 / 分辨率 / 卸载干净 / 文件落盘 / 进程状态…）；只判结论报告，不判过程步骤（过程是否执行由工具链证据另行判定，你只看最终结论）；
2. 任务参数类关键值必须与【任务题目】一致（题目说验证码是 X，结论就得是 X，不能凭空改值或漏值）；稳定设备属性（主机名/系统版本/用户/serialno）与【参考结论】一致；易变项（分辨率/软件版本号/设备台数）如实报告当前值即可，不与参考硬比对；
3. 只提过程、漏关键项、或报了错误值 → pass=false；
4. 参考结论用于对照"完整度/格式"与稳定设备属性，不参与任务参数与易变项的硬比对。'''


def _call_messages(base_url: str, token: str, model: str, prompt: str,
                   max_tokens: int = 4096, timeout: int = 120) -> str:
    """调 Anthropic Messages API，返回所有 text 块拼接（跳过 thinking 块）。"""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/messages", data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("x-api-key", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    parts = []
    for b in data.get("content", []):
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif isinstance(b, str):
            parts.append(b)
    return "".join(parts)


def _parse_pass(text: str):
    """从文本里抠 JSON，解析 pass/reason；兼容 markdown 代码块与前后缀。"""
    t = text or ""
    t = re.sub(r"```(?:json)?", "", t)
    # 找第一个平衡的 { ... }（支持嵌套，用简单扫描）
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    d = json.loads(t[start:i + 1])
                except Exception:
                    return None
                if "pass" not in d:
                    return None
                return d
    return None


def judge(query: str, expected_answer: dict, reference: str, actual: str,
          model: str = None, max_tokens: int = None,
          base_url: str = None, token: str = None) -> dict:
    """判最终结论。返回 {"pass": bool, "reason": str}；网关失败抛异常。"""
    conf = _load_conf()
    model = model or _override["model"] or conf["model"]
    max_tokens = max_tokens or _override["max_tokens"] or _DEFAULT_MAX_TOKENS
    base_url = base_url or conf["base_url"]
    token = token or conf["token"]

    ea = expected_answer if isinstance(expected_answer, dict) else {}
    expected = ea.get("result") or ""

    prompt = _PROMPT.format(
        query=query or "",
        expected=expected,
        reference=(reference or "").strip(),
        actual=(actual or "").strip(),
    )

    last_err = None
    for _ in range(2):  # 解析失败重试 1 次
        text = _call_messages(base_url, token, model, prompt, max_tokens=max_tokens)
        d = _parse_pass(text)
        if d is not None:
            return {
                "pass": bool(d.get("pass")),
                "reason": str(d.get("reason", ""))[:80],
            }
        last_err = f"parse_fail: {text[:120]!r}"
    raise RuntimeError(last_err or "judge_llm 无响应")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 自测：判一个简单结论
    r = judge(
        query="在设备 X 上配置 UU 取号验证码 163a163a 并写 DK 备注",
        expected_answer={"result": "取得真实格式的设备 ID，验证码写入 DK 备注且可回读一致", "process": []},
        reference="设备 ID=123456，验证码=163a163a，DK 备注已写入",
        actual="全部完成。设备 ID=697967673，验证码=163a163a，DK 备注已写入。需要释放设备吗？",
    )
    print(r)
