# -*- coding: utf-8 -*-
"""Dify Chatflow 黑盒客户端：驱动多轮对话 + 端点自动回退。

把 Dify 上搭好的 Chatflow 当作黑盒。多轮连续性由 Dify 服务端维护，
客户端只需要每轮带着**同一个 user** 和**上一轮返回的 conversation_id**。
"""
from __future__ import annotations

import time
from typing import Optional

import requests


class DifyError(RuntimeError):
    """带状态码与后端 code/message 的可读错误。

    route_error=True 表示“这个 URL 路径不存在”（可换端点重试），
    其余一律为业务/鉴权错误，直接上抛。
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 code: Optional[str] = None, detail: Optional[str] = None,
                 route_error: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail
        self.route_error = route_error


# 不同应用类型可能的消息端点（顺序 = 尝试顺序）。
# 绝大多数版本 Chatflow 走 /chat-messages；部分版本暴露 /chatflow/chat-messages。
# 只有遇到“路由不存在”才会切下一个，成功后记住，保证同一次对话路径稳定。
ENDPOINT_CANDIDATES = {
    "chatflow": ["/chat-messages", "/chatflow/chat-messages"],
    "chat": ["/chat-messages"],
}


class DifyClient:
    def __init__(self, base_url: str, api_key: str, app_type: str = "chatflow",
                 endpoint: Optional[str] = None, timeout: int = 300, retries: int = 3):
        if not base_url:
            raise DifyError(
                "缺少 DIFY_BASE_URL。请在 Dify 应用「API 访问」页复制 Base URL"
                "（例如 https://api.dify.ai/v1 或 http://<自托管域名>/v1）填入 .env。"
            )
        if not api_key:
            raise DifyError(
                "缺少 DIFY_API_KEY。请在 Dify 应用「API 访问」→「API 密钥」"
                "生成新密钥（形如 app-xxxx）填入 .env。"
            )
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.timeout = timeout
        self.retries = retries
        candidates = ([endpoint] if endpoint
                      else ENDPOINT_CANDIDATES.get(app_type, ENDPOINT_CANDIDATES["chat"]))
        self._pending = [self.base_url + p for p in candidates]
        self._url: Optional[str] = None  # 探测成功后固定

    # ---------------- 对外主入口 ----------------
    def chat(self, query: str, user: str, conversation_id: Optional[str] = None,
             inputs: Optional[dict] = None) -> dict:
        """发送一轮消息。

        返回 dict：
          answer         助手文本回答
          conversation_id 下一轮要回传的会话 id
          message_id      本轮消息 id（可用于后续对照 Dify 日志）
          latency_s       本轮耗时（秒）
          meta            Dify 返回的 metadata（检索引用等，若有）
        """
        payload = {
            "inputs": inputs or {},
            "query": query,
            "response_mode": "blocking",
            "user": user,
            "conversation_id": conversation_id or "",
        }
        data, latency = self._post_with_fallback(payload)

        answer = data.get("answer", "")
        if not isinstance(answer, str):
            answer = str(answer)
        new_cid = data.get("conversation_id") or conversation_id or ""
        return {
            "answer": answer,
            "conversation_id": new_cid,
            "message_id": data.get("message_id"),
            "latency_s": round(latency, 2),
            "meta": data.get("metadata") or {},
        }

    # ---------------- 内部实现 ----------------
    def _post_with_fallback(self, payload: dict):
        """按候选端点依次尝试；仅当“路由不存在(404/405)”时换下一个。"""
        if self._url:
            return self._post(self._url, payload)

        last_route: Optional[DifyError] = None
        while self._pending:
            url = self._pending.pop(0)
            try:
                data, latency = self._post(url, payload)
            except DifyError as e:
                if e.route_error:
                    last_route = e
                    continue
                raise
            self._url = url  # 记住可用端点
            return data, latency

        raise DifyError(
            f"所有候选端点都不存在（最近一次：{last_route}）。"
            "请在 Dify 应用「API 访问」页查看 curl 样例里的真实路径，"
            "写入 .env 的 DIFY_CHAT_ENDPOINT。"
        )

    def _post(self, url: str, payload: dict):
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            t0 = time.monotonic()
            try:
                resp = requests.post(url, headers=self.headers, json=payload,
                                     timeout=self.timeout)
            except requests.RequestException as e:  # 网络层错误可重试
                last_exc = e
                if attempt == self.retries:
                    break
                time.sleep(min(2 ** attempt, 8))
                continue

            latency = time.monotonic() - t0
            if resp.status_code == 200:
                try:
                    return resp.json(), latency
                except ValueError:
                    raise DifyError("接口返回不是合法 JSON", status=200)

            code, msg = self._error_fields(resp)

            # 429 限流：按 Retry-After 等待后重试
            if resp.status_code == 429:
                if attempt == self.retries:
                    break
                wait = float(resp.headers.get("Retry-After", "2") or 2)
                time.sleep(min(wait, 10))
                continue

            raise DifyError(
                self._friendly(resp.status_code, code, msg),
                status=resp.status_code, code=code, detail=msg,
                route_error=self._is_route_missing(resp.status_code, code, msg),
            )
        raise DifyError(f"请求多次失败，最后一次异常：{last_exc}")

    # ---------------- 工具方法 ----------------
    @staticmethod
    def _error_fields(resp: requests.Response):
        try:
            body = resp.json()
            return body.get("code"), body.get("message") or body.get("error")
        except ValueError:
            return None, resp.text[:300]

    @staticmethod
    def _is_route_missing(status: int, code: Optional[str], msg) -> bool:
        if status not in (404, 405):
            return False
        # conversation 不存在也返回 404，但那是业务错误，不该触发换端点
        if code == "not_found" and msg and "Conversation" in str(msg):
            return False
        return True

    @staticmethod
    def _friendly(status: int, code: Optional[str], msg) -> str:
        hint = {
            401: "鉴权失败：检查 DIFY_API_KEY 是否复制完整，且应用已「发布」。",
            403: "无权访问该应用：确认 API key 属于这个应用。",
        }.get(status, "")
        if code == "not_found" and msg and "Conversation" in str(msg):
            return (f"conversation_id 不存在/已失效（{msg}）。"
                    "通常是因为流程重新发布过，请对每个场景都新开一轮对话。")
        detail = str(msg or "")
        return (f"HTTP {status} {detail}".strip()
                + (f"；{hint}" if hint else ""))
