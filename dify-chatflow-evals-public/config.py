# -*- coding: utf-8 -*-
"""配置加载：读取项目根目录下的 .env（或显式指定的文件）。

设计要点：
  * .env 的默认位置固定在 config.py 所在目录（即项目根），
    与“当前工作目录”无关 —— 这样无论在 Jupyter、终端、任意 cwd 下运行都能读到。
  * 自带极简 .env 解析，不依赖 python-dotenv。
  * 已存在的环境变量优先于 .env 文件（便于在 CI / 服务器里直接注入）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "DIFY_BASE_URL": "",
    "DIFY_API_KEY": "",
    "DIFY_APP_TYPE": "chatflow",
    "DIFY_CHAT_ENDPOINT": "",
    "REQUEST_TIMEOUT": "300",
    "REQUEST_RETRIES": "3",
}


@dataclass
class Config:
    dify_base_url: str           # 形如 https://api.dify.ai/v1（含 /v1）
    dify_api_key: str            # 形如 app-xxxxxxxx
    app_type: str = "chatflow"   # chatflow | chat
    endpoint: str | None = None  # 消息端点；None = 自动回退
    timeout: int = 300
    retries: int = 3
    env_path: str = ""           # 实际读取的 .env 绝对路径（调试用）


def _parse_env_file(path: str) -> None:
    """极简 .env 解析：KEY=VALUE。支持 # 注释、export 前缀、引号包裹、值首尾空格。"""
    if not path or not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            # 已有“非空”环境变量优先（便于 CI 注入）；残留的空字符串会被 .env 覆盖，
            # 否则可能出现“文件里有值却读到空”的诡异问题（旧 dotenv 写入的空串）。
            if key and (key not in os.environ or not os.environ.get(key, "").strip()):
                os.environ[key] = val


def load_config(env_file: Optional[str] = None) -> Config:
    # 未显式指定 -> 固定读项目根的 .env；指定了则按“相对当前目录”解析
    if env_file:
        env_path = os.path.abspath(env_file)
    else:
        env_path = os.path.join(PROJECT_DIR, ".env")
    _parse_env_file(env_path)

    def _get(key: str) -> str:
        return os.environ.get(key, DEFAULTS[key]).strip()

    endpoint = _get("DIFY_CHAT_ENDPOINT") or None
    return Config(
        dify_base_url=_get("DIFY_BASE_URL").rstrip("/"),
        dify_api_key=_get("DIFY_API_KEY"),
        app_type=_get("DIFY_APP_TYPE").lower(),
        endpoint=endpoint,
        timeout=int(_get("REQUEST_TIMEOUT")),
        retries=int(_get("REQUEST_RETRIES")),
        env_path=env_path,
    )
