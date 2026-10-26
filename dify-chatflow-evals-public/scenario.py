# -*- coding: utf-8 -*-
"""评测场景（case）的结构定义与加载。

一个 case = 一个“固定剧本”的多轮对话：
  persona / goal / expectations / forbidden 是给评分人（人工或后续的
  LLM-judge）看的上下文；turns 是用户侧的固定话术。agent 按真实接口
  跑完整个 turns，得到 transcript。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Scenario:
    id: str
    persona: str = ""
    goal: str = ""
    turns: List[str] = field(default_factory=list)
    expectations: List[str] = field(default_factory=list)
    forbidden: List[str] = field(default_factory=list)
    note: str = ""


def load_scenarios(path: str) -> List[Scenario]:
    """path 可以是单个 .json 文件，或目录（读取其中所有 *.json）。

    文件内容允许三种形态：
      1) 单个场景对象
      2) 数组  [...场景对象...]
      3) 对象 {"scenarios": [...场景对象...]}
    """
    files: List[str] = []
    if os.path.isdir(path):
        files = sorted(os.path.join(path, f)
                       for f in os.listdir(path) if f.endswith(".json"))
    else:
        files = [path]

    out: List[Scenario] = []
    seen = set()
    for fp in files:
        with open(fp, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        items = raw.get("scenarios", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            items = [raw]
        for it in items:
            sc = _parse_one(it, fp)
            if sc.id in seen:
                raise ValueError(f"场景 id 重复：{sc.id}（来自 {fp}）")
            seen.add(sc.id)
            out.append(sc)
    return out


def _parse_one(it: dict, src: str) -> Scenario:
    sc_id = str(it.get("id", "")).strip()
    if not sc_id:
        raise ValueError(f"{src} 中存在缺少 id 的场景")

    turns = it.get("turns")
    if not isinstance(turns, list) or not turns or not all(
            isinstance(t, str) and t.strip() for t in turns):
        raise ValueError(f"场景 {sc_id}（{src}）的 turns 必须是非空字符串列表")

    return Scenario(
        id=sc_id,
        persona=str(it.get("persona", "")).strip(),
        goal=str(it.get("goal", "")).strip(),
        turns=[t.strip() for t in turns],
        expectations=[str(x).strip() for x in it.get("expectations", [])],
        forbidden=[str(x).strip() for x in it.get("forbidden", [])],
        note=str(it.get("note", "")).strip(),
    )
