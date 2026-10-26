# -*- coding: utf-8 -*-
"""固定剧本多轮评测 runner（脚手架第 1-2 步）。

把 Dify Chatflow 当黑盒跑完每个场景，得到 transcript：
  * 保存 JSONL（机器可读，供后续 LLM-judge / 回归 diff）
  * 可选导出 Markdown（人类可读，供人工打标/校准打分）
  * 终端打印摘要

用法（在项目根目录下执行）：
    python run_eval.py
    python run_eval.py --scenarios scenarios/example_career_transfer.json
    python run_eval.py --repeat 3 --markdown
    python run_eval.py --help
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import List

from config import load_config
from dify_client import DifyClient, DifyError
from scenario import Scenario, load_scenarios


def run_scenario(client: DifyClient, sc: Scenario) -> dict:
    """跑一个场景的完整多轮对话。返回 dict：
    {scenario_id, user, ok, error, turns, elapsed_s}
    """
    # 每次跑都新建 user：Dify 以 (conversation_id, user) 维护上下文，
    # 用随机 user 可避免不同场景/不同次运行之间互相污染。
    user = f"eval-{uuid.uuid4().hex[:12]}"
    conv_id = ""
    turns: List[dict] = []
    t0 = time.monotonic()
    error = None

    for i, query in enumerate(sc.turns, start=1):
        try:
            res = client.chat(query=query, user=user, conversation_id=conv_id)
        except DifyError as e:
            error = f"第 {i} 轮失败：{e}"
            turns.append({"turn": i, "user": query, "assistant": None, "error": str(e)})
            break

        turns.append({
            "turn": i,
            "user": query,
            "assistant": res["answer"],
            "message_id": res.get("message_id"),
            "conversation_id": res.get("conversation_id"),
            "latency_s": res.get("latency_s"),
        })
        conv_id = res["conversation_id"]

    return {
        "scenario_id": sc.id,
        "user": user,
        "ok": error is None and len(turns) == len(sc.turns),
        "error": error,
        "turns": turns,
        "elapsed_s": round(time.monotonic() - t0, 2),
    }


def to_markdown(sc: Scenario, result: dict) -> str:
    """导出人类可读的 transcript，方便人工打分（第 3 步校准用）。"""
    lines = [
        f"# 场景：{sc.id}",
        "",
        f"- **状态**：{'ok' if result['ok'] else 'FAILED'}  "
        f"（{len(result['turns'])}/{len(sc.turns)} 轮）",
    ]
    if sc.persona:
        lines.append(f"- **用户画像**：{sc.persona}")
    if sc.goal:
        lines.append(f"- **目标**：{sc.goal}")
    if sc.expectations:
        lines.append("- **期望**：" + "；".join(sc.expectations))
    if sc.forbidden:
        lines.append("- **禁止**：" + "；".join(sc.forbidden))
    lines.append("")
    for tr in result["turns"]:
        lines.append(f"### 第 {tr['turn']} 轮")
        lines.append(f"**【用户】** {tr['user']}")
        if tr.get("assistant"):
            lines.append(f"**【助手】** {tr['assistant']}")
        elif tr.get("error"):
            lines.append(f"**【助手】** *（出错：{tr['error']}）*")
        lines.append("")
    return "\n".join(lines)


def save_result(out_dir: str, tag: str, sc: Scenario, result: dict,
                markdown: bool) -> str:
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, sc.id)
    if tag:
        stem += f".{tag}"

    with open(stem + ".jsonl", "w", encoding="utf-8") as fh:
        header = {
            "type": "meta",
            "scenario_id": sc.id,
            "persona": sc.persona,
            "goal": sc.goal,
            "expectations": sc.expectations,
            "forbidden": sc.forbidden,
            "user": result["user"],
            "ok": result["ok"],
            "run_at": datetime.now().isoformat(timespec="seconds"),
        }
        fh.write(json.dumps(header, ensure_ascii=False) + "\n")
        for tr in result["turns"]:
            fh.write(json.dumps(tr, ensure_ascii=False) + "\n")

    if markdown:
        with open(stem + ".md", "w", encoding="utf-8") as fh:
            fh.write(to_markdown(sc, result))
    return stem


def main(argv: List[str] | None = None) -> int:
    # Windows 控制台默认 GBK 无法编码 ▶/↳ 等字符，统一重配为 UTF-8（失败则忽略）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(
        description="Dify Chatflow 固定剧本多轮评测 runner")
    parser.add_argument("--scenarios", default="scenarios",
                        help="场景文件或目录（默认 scenarios/）")
    parser.add_argument("--out", default="transcripts",
                        help="输出目录（默认 transcripts/）")
    parser.add_argument("--env", default=None,
                        help=".env 文件路径（默认：项目根目录的 .env，与当前目录无关）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每个场景重复跑 N 次，测稳定性（默认 1）")
    parser.add_argument("--tag", default="",
                        help="附加到输出文件名的标记，例如 --tag v2")
    parser.add_argument("--markdown", action="store_true",
                        help="额外导出人类可读 .md（供人工打分/校准）")
    args = parser.parse_args(argv)

    # ---- 配置与客户端 ----
    cfg = load_config(args.env)
    try:
        client = DifyClient(cfg.dify_base_url, cfg.dify_api_key,
                            app_type=cfg.app_type, endpoint=cfg.endpoint,
                            timeout=cfg.timeout, retries=cfg.retries)
    except DifyError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        print(f"（本次读取的 .env 路径：{cfg.env_path}；"
              f"如为空说明该文件不存在或没写到变量）", file=sys.stderr)
        return 2

    # ---- 场景加载 ----
    scenarios = load_scenarios(args.scenarios)
    if not scenarios:
        print(f"[错误] 在 {args.scenarios} 下没有找到可用场景", file=sys.stderr)
        return 2

    print(f"共 {len(scenarios)} 个场景 × {args.repeat} 次，输出到 {args.out} ...")
    ok_count = 0
    total = len(scenarios) * args.repeat

    for sc in scenarios:
        title = sc.persona or sc.goal or ""
        print(f"\n▶ {sc.id}  | {len(sc.turns)} 轮 | {title}")
        for rep in range(1, args.repeat + 1):
            tag = args.tag if args.repeat == 1 else f"{rep:02d}"
            result = run_scenario(client, sc)
            stem = save_result(args.out, tag, sc, result, args.markdown)
            status = "ok" if result["ok"] else "FAILED"
            print(f"    run#{rep}  {status}  {len(result['turns'])}/{len(sc.turns)} 轮"
                  f"  {result['elapsed_s']}s  ->  {os.path.basename(stem)}")
            if result["ok"]:
                ok_count += 1
            elif result["error"]:
                print(f"        ↳ {result['error']}")

    print(f"\n=== 完成：{ok_count}/{total} 次跑通 ===")
    print("transcript 已落盘。下一步：人工给这些 transcript 打分 → 校准 LLM-judge。")
    return 0 if ok_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
