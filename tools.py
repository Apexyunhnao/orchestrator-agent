"""
子Agent工具 — 将 RAG Agent 和客服工单 Agent 包装为 LangChain Tool。
供 Supervisor 多轮决策时调用。
"""

import time
from typing import Any

import httpx
from langchain_core.tools import tool

# ── 常量 ──────────────────────────────────────────────

RAG_URL = "http://127.0.0.1:8002/query"
TICKET_URL = "http://127.0.0.1:8001/api/ticket"
TIMEOUT_S = 15.0
RETRY_DELAY_S = 2.0
MAX_RETRIES = 1  # 首次失败后重试 1 次（共 2 次调用）
ANSWER_MAX_CHARS = 500


# ── 通用 HTTP POST ────────────────────────────────────

def _is_retryable(exc: Exception, status_code: int | None) -> bool:
    """仅网络/超时/5xx 可重试，4xx 不重试。"""
    if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError)):
        return True
    if status_code is not None and status_code >= 500:
        return True
    return False


def _http_post_with_retry(url: str, json_data: dict[str, Any]) -> httpx.Response:
    """POST 请求，带超时 + 重试。"""
    last_exc: Exception | None = None
    last_status: int | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            # 绕过系统代理，直连本地服务
            with httpx.Client(trust_env=False) as client:
                resp = client.post(url, json=json_data, timeout=TIMEOUT_S)
            if resp.status_code < 500:
                return resp
            last_status = resp.status_code
        except httpx.HTTPError as e:
            last_exc = e
            last_status = None

        if attempt == MAX_RETRIES:
            break
        if not _is_retryable(last_exc or httpx.HTTPError(""), last_status):
            break
        time.sleep(RETRY_DELAY_S)

    detail = f"status={last_status}" if last_status else str(last_exc)
    print(f"[tools] POST {url} 失败（{MAX_RETRIES} 次重试后）: {detail}")
    raise (last_exc or httpx.HTTPError(f"请求失败, status={last_status}"))


# ── Tool 定义 ──────────────────────────────────────────

@tool
def query_rag(question: str) -> str:
    """查询客服政策知识库。用于查退货政策、退款规则、换货条件、物流时效等电商售后政策问题。
    参数 question: 用户想问的具体政策问题，例如"退货需要什么条件"。"""
    print(f"[tools] query_rag 调用: {question[:80]}")
    try:
        resp = _http_post_with_retry(RAG_URL, {"question": question})
        data = resp.json()
        answer = data.get("answer", "")
        if answer:
            return answer[:ANSWER_MAX_CHARS]
        # 某些情况下 answer 为空但有 error
        return data.get("error", "知识库返回为空")
    except Exception as e:
        print(f"[tools] query_rag 失败: {e}")
        return "知识库暂不可用"


@tool
def create_ticket(message: str, user_identifier: str = "") -> str:
    """创建客服工单。用于订单查询、改收货地址、退差价、催物流、取消订单、售后维修等具体业务操作。
    参数 message: 用户的操作请求描述，例如"查一下ORD-1003的订单状态"。
    参数 user_identifier: 可选，用户标识（工号/手机号）。"""
    print(f"[tools] create_ticket 调用: {message[:80]}")
    try:
        resp = _http_post_with_retry(
            TICKET_URL,
            {"message": message, "user_identifier": user_identifier},
        )
        data = resp.json()
        return (
            f"工单ID: {data.get('ticket_id', '-')}\n"
            f"分类: {data.get('category', '-')}\n"
            f"状态: {data.get('status', '-')}\n"
            f"处理结果: {data.get('resolution', '') or data.get('error', '-')}"
        )
    except Exception as e:
        print(f"[tools] create_ticket 失败: {e}")
        return "工单服务暂不可用"
