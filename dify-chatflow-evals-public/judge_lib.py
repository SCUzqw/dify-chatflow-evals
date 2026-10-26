# -*- coding: utf-8 -*-
"""LLM-judge 管道：judge 后端 = Dify 独立 Workflow（工作流应用）。

调用链：
  transcript.md --(parse_transcript_md)--> 轮次 --(strip_think)--> 干净正文
      + gold.json 里的单个维度 --(build_task)--> 评测任务文本
      --(call_judge: POST /workflows/run)--> {score, reason}

设计要点：
  * 每个维度单独一次 judge 调用（逐维打分，避免光环效应）。
  * judge 收到的是“已剔除 <think>/<!--dify-deepseek-reasoning--> 推理”的正文，
    与被测侧（金标准人工也忽略 think）口径一致。
  * “正文是否泄漏标签”（gold 里的 U2 行）不交给 LLM，而是用本模块的确定性检查
    在 run_calibration 里直接判定，避免“把已剥离的文本再拿去问有没有标签”的循环。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

import requests

# 保证可以从项目根直接 import（无论从哪启动）
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

THINK_HTML_RE = re.compile(r"<!--.*?-->", re.S)
THINK_TAG_RE = re.compile(r"<think[^>]*>.*?</think>", re.S)
LONE_THINK_RE = re.compile(r"</?think\b[^>]*>", re.I)
LEAK_TAG_RE = re.compile(r"<[a-zA-Z/!][^>]*>")  # 剥离 think 后正文里残留的标签 → 泄漏
HEADING_RE = re.compile(r"^#{2,}\s*第\s*\d+\s*轮.*?$", re.M)


def strip_think(text: str) -> str:
    """去掉 HTML 注释、<think>…</think>，并清理前导空行/残留的空标签壳。"""
    t = THINK_HTML_RE.sub("", text)
    t = THINK_TAG_RE.sub("", t)
    t = LONE_THINK_RE.sub("", t)
    t = re.sub(r"^\s*[\n\r]+", "", t)
    return t.strip()


def has_leakage(text: str) -> bool:
    """判断“正式正文”里是否仍残留标签（即推理内容泄漏到可见回复）。"""
    return bool(LEAK_TAG_RE.search(text))


def _line_after(marker: str, s: str) -> str:
    i = s.find(marker)
    if i == -1:
        return ""
    rest = s[i + len(marker):]
    line = rest.splitlines()[0] if rest.splitlines() else ""
    return line.strip().lstrip("*").strip()


def parse_transcript_md(path: str) -> list[dict]:
    """解析 transcripts/*.md 或 judge/neg/*.md（格式一致：### 第 N 轮 + 用户/助手标签）。

    返回每轮 {'user', 'assistant'(已剥离think), 'raw'(剥离前原文, 供 U2 泄漏检查)}。
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    chunks = HEADING_RE.split(text)  # [头部, 第1轮, 第2轮, ...]
    turns: list[dict] = []
    for chunk in chunks[1:]:
        user = _line_after("【用户】", chunk)
        idx = chunk.find("**【助手】**")
        if idx == -1:
            idx = chunk.find("【助手】")
        if idx == -1:
            raw = ""
        else:
            raw = chunk[idx + len("**【助手】**"):].strip()
        if not user and not raw:
            continue
        turns.append({"user": user, "assistant": strip_think(raw), "raw": raw})
    return turns


def build_task(turns: list[dict], row: dict) -> str:
    """把一个维度的问题 + 整段对话拼成一次 judge 任务。"""
    lines = [
        "你是严谨的评测员。请按下面给出的『一个维度』给被测助手在整段对话中的表现打分。",
        "",
        "——对话实录（助手回复中的 <think> 等推理内容已剔除，只保留正式回复）——",
    ]
    for i, t in enumerate(turns, 1):
        lines.append(f"第{i}轮")
        lines.append(f"用户：{t['user']}")
        lines.append(f"助手：{t['assistant']}")
    lines += [
        "",
        "——本次要打分的维度（只评这一条，忽略其他好坏）——",
        f"维度类型：{row.get('kind', 'expectation')}",
        f"判定问题：{row['question']}",
        "评分刻度：2=完全达成；1=部分达成或轻微偏差；0=未达成。"
        "（禁止类维度：2=全程未出现该行为；1=边缘/疑似；0=实际出现、违反）",
        "输出要求：1) reason 一句话，尽量引用关键轮次原文（≤25字）；"
        "2) 只输出一行 JSON，禁止输出任何 JSON 以外的文字、代码块或解释。",
        'JSON 格式：{"score": 0|1|2, "reason": "..."}',
    ]
    return "\n".join(lines)


class JudgeError(Exception):
    pass


_ENDPOINTS = ["/workflows/run", "/workflow-runs"]


def call_judge(task: str, base_url: str, api_key: str,
               timeout: int = 300, retries: int = 3, user: str = "judge-eval") -> dict:
    """调用 Dify judge Workflow，返回 {'score': 0|1|2, 'reason': str}。"""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        for ep in _ENDPOINTS:
            url = base_url.rstrip("/") + ep
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json={"inputs": {"task": task},
                          "response_mode": "blocking",
                          "user": user},
                    timeout=timeout,
                )
            except Exception as exc:  # 网络层错误
                last_err = exc
                continue
            if resp.status_code in (404, 405) and ep is not _ENDPOINTS[-1]:
                continue  # 端点不存在 → 试下一个
            if resp.status_code >= 400:
                last_err = JudgeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                continue
            data = resp.json().get("data", {})
            raw = _extract_output(data)
            if not raw:
                last_err = JudgeError("judge 响应中没有可取文本")
                continue
            parsed = _parse_score(raw)
            if parsed:
                return parsed
            last_err = JudgeError("judge 输出不是合法 {score,reason} JSON: " + raw[:200])
        if attempt < retries:
            time.sleep(1.5 * attempt)
    raise JudgeError(f"judge 调用失败（重试 {retries} 次后）：{last_err}")


def call_judge_vote(task: str, base_url: str, api_key: str,
                    timeout: int = 300, retries: int = 3,
                    n: int = 3, user: str = "judge-eval") -> dict:
    """同一维度独立判 n 次、取多数票。返回 {score, reason, counts, agree, errors}。

    动机：judge 模型即使温度=0 仍可能有采样抖动（已观测到同一行跨轮在 0/1/2
    之间翻转），单次结果不能当作稳定判定。n 次投票后：
      * agree=True ：某一分数票数过半 → score 取该分数，作为最终判定。
      * agree=False：无过半分数（如 0/1/2 各一票）→ score=None，行标 SPLIT 需人工看。
    """
    counts = {0: 0, 1: 0, 2: 0}
    reasons: dict[int, str] = {}
    errors = 0
    for _ in range(max(1, n)):
        try:
            r = call_judge(task, base_url, api_key, timeout, retries, user=user)
            counts[r["score"]] += 1
            reasons.setdefault(r["score"], r["reason"])
        except JudgeError:
            errors += 1
    if sum(counts.values()) == 0:
        raise JudgeError("judge 全部调用失败，无法投票")
    top = max(counts, key=lambda s: (counts[s], s))  # 同票取高分（偏严侧）
    agree = n == 1 or counts[top] * 2 > n
    return {
        "score": top if agree else None,
        "reason": reasons.get(top, ""),
        "counts": counts,
        "agree": agree,
        "errors": errors,
    }


def _extract_output(data: dict):
    outputs = data.get("outputs") or {}
    if isinstance(outputs, dict):
        for val in outputs.values():
            if isinstance(val, str) and val.strip():
                return val
    ans = data.get("answer")  # 兜底：若是 chat 型应用
    if isinstance(ans, str) and ans.strip():
        return ans
    return None


def _parse_score(raw: str):
    try:
        s = raw[raw.find("{"):raw.rfind("}") + 1]
        obj = json.loads(s)
        sc = int(obj["score"])
        if sc in (0, 1, 2):
            return {"score": sc, "reason": str(obj.get("reason", ""))[:500]}
    except Exception:
        return None
    return None


def env_for_judge():
    """读取 judge 配置。KEY 必填；BASE_URL 缺省复用被测 app 的 base；超时/重试复用。"""
    from config import load_config
    cfg = load_config()
    key = os.environ.get("DIFY_JUDGE_API_KEY", "").strip()
    base = (os.environ.get("DIFY_JUDGE_BASE_URL", "").strip() or cfg.dify_base_url).rstrip("/")
    return base, key, cfg.timeout, cfg.retries


if __name__ == "__main__":
    # 自检：python judge_lib.py transcripts/memory_001_planted_facts_consistency.md
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("用法：python judge_lib.py <transcript.md>")
        sys.exit(1)
    ts = parse_transcript_md(path)
    print(f"解析到 {len(ts)} 轮：")
    for i, t in enumerate(ts, 1):
        leaked = "泄漏!" if has_leakage(t["assistant"]) else "干净"
        print(f"  第{i}轮  用户:{t['user'][:24]!r}  助手正文前40字:{t['assistant'][:40]!r}  [{leaked}]")
