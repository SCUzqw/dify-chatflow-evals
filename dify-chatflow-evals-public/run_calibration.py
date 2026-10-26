# -*- coding: utf-8 -*-
"""金标准对拍：让 LLM-judge 对 正例+负例 的全部维度打分，与 judge/gold.json 的金标准比较。

用法（项目根目录）：
    python run_calibration.py                          # 全部场景
    python run_calibration.py memory_001_planted_facts_consistency   # 只看某场景
    python run_calibration.py --repeat 3               # 每行 judge 判 3 次取多数票（默认）
    python run_calibration.py --repeat 1               # 每行只判 1 次（快速、不稳，仅排查用）

判定：
    PASS     judge 多数票 == 金标
    LENIENT  judge 判得比金标高（典型：负例没拦住 → judge 太松）
    STRICT   judge 判得比金标低（典型：正例被误伤 → judge 太严）
    ERROR    judge 调用/解析失败
    SPLIT    judge 多次投票无过半多数（自相矛盾，需人工看）

约定：
    * U2（正文泄漏）不走 LLM —— 用 judge_lib.has_leakage 对剥离后的正文做确定性检查。
    * 其余行走 judge，默认每行判 --repeat 次取多数票：judge 模型即使温度=0 仍有采样抖动
      （实测同一行跨轮可在 0/1/2 间翻转），单次结果不可当作稳定判定，勿用 1 次做结论。
退出码：0 = 全部一致；1 = 有失配；2 = 配置错误（缺 DIFY_JUDGE_API_KEY）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from judge_lib import (parse_transcript_md, build_task, call_judge,
                       call_judge_vote, env_for_judge, has_leakage, JudgeError)

ROOT = os.path.dirname(os.path.abspath(__file__))
GOLD_PATH = os.path.join(ROOT, "judge", "gold.json")


def main(argv: list[str]) -> int:
    # Windows 控制台/重定向默认 GBK，打印中文会乱码 → 统一 UTF-8
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="金标准对拍：LLM-judge 多数票 vs judge/gold.json")
    parser.add_argument("scenario", nargs="?", default=None,
                        help="只看某场景，如 memory_001_planted_facts_consistency")
    parser.add_argument("--repeat", type=int, default=3,
                        help="每行 judge 判 N 次取多数票（默认 3）")
    args = parser.parse_args(argv)
    only = args.scenario
    n_vote = max(1, args.repeat)

    base_url, api_key, timeout, retries = env_for_judge()
    if not api_key:
        print("[配置错误] 缺少 DIFY_JUDGE_API_KEY。请先在 Dify 建好 judge Workflow 并发布，")
        print("        把它的 app key 填入项目根 .env（步骤见 judge/BUILD_DIFY_JUDGE.md）。")
        return 2

    with open(GOLD_PATH, encoding="utf-8") as fh:
        gold = json.load(fh)

    total = passed = 0
    stats = {"LENIENT": 0, "STRICT": 0, "ERROR": 0, "SPLIT": 0}
    mismatches: list[tuple[str, str, int, int | str]] = []

    for case in gold["cases"]:
        sid = case["scenario_id"]
        if only and sid != only:
            continue
        tpath = os.path.join(ROOT, case["transcript"])
        if not os.path.isfile(tpath):
            print(f"[跳过] 找不到 transcript: {tpath}")
            continue
        turns = parse_transcript_md(tpath)
        if not turns:
            print(f"[跳过] 无法解析轮次: {tpath}")
            continue
        print(f"\n== {sid}  （{len(turns)} 轮, {len(case['rows'])} 维, 金标={case.get('expected_label', '?')}）")

        for row in case["rows"]:
            exp = row["expected"]
            total += 1
            if row.get("kind") == "code" or row["row_id"] == "U2":
                # 确定性检查，不走 LLM
                leak = any(has_leakage(t["assistant"]) for t in turns)
                got = 0 if leak else 2
                tag = "PASS " if got == exp else ("STRICT " if got < exp else "LENIENT")
                _count(tag, stats)
                if got == exp:
                    passed += 1
                else:
                    mismatches.append((sid, row["row_id"], exp, got))
                print(f"  {row['row_id']:<4} {tag}  金标={exp} 判定={got}  [code 检查]")
                continue

            task = build_task(turns, row)
            try:
                vote = call_judge_vote(task, base_url, api_key, timeout,
                                       retries, n=n_vote)
            except JudgeError as exc:
                print(f"  {row['row_id']:<4} ERROR  {exc}")
                stats["ERROR"] += 1
                mismatches.append((sid, row["row_id"], exp, "ERROR"))
                continue

            counts = vote["counts"]
            if vote["score"] is None:
                # 多数票不收敛：judge 自相矛盾，标 SPLIT 需人工看
                got: int | str = "SPLIT"
                got_txt = "SPLIT"
                tag = "SPLIT"
                stats["SPLIT"] += 1
                mismatches.append((sid, row["row_id"], exp, "SPLIT"))
            else:
                got = vote["score"]
                got_txt = str(got)
                if got == exp:
                    tag = "PASS "
                    passed += 1
                elif got > exp:
                    tag = "LENIENT"
                    stats["LENIENT"] += 1
                else:
                    tag = "STRICT "
                    stats["STRICT"] += 1
                if got != exp:
                    mismatches.append((sid, row["row_id"], exp, got))
            agree = "一致" if vote["agree"] else "分歧"
            print(f"  {row['row_id']:<4} {tag}  金标={exp} "
                  f"judge={got_txt}  票{cnt_str(counts)}[{agree}]"
                  f"{'' if n_vote == 1 else ' x' + str(n_vote)}")

    print(f"\n=== 汇总：{passed}/{total} 与金标一致 ===")
    print("失配：LENIENT(太松,负例没拦住) %d | STRICT(太严,误伤正例) %d | "
          "SPLIT(自相矛盾) %d | ERROR %d"
          % (stats["LENIENT"], stats["STRICT"], stats["SPLIT"],
             stats["ERROR"]))
    if n_vote > 1:
        print(f"（每行判 {n_vote} 次取多数票）")
    if mismatches:
        print("\n失配明细（scenario  row  金标 -> judge）：")
        for sid, rid, exp, got in mismatches:
            print(f"  {sid:<38} {rid:<4} {exp} -> {got}")
        return 1
    return 0


def _count(tag: str, stats: dict) -> None:
    k = tag.strip()
    if k in stats:
        stats[k] += 1


def cnt_str(counts: dict[int, int]) -> str:
    """把票数 {0:1,1:1,2:1} 缩成 '0/1/2=1/1/1'。"""
    return "0/1/2=" + "/".join(str(counts[i]) for i in (0, 1, 2))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
