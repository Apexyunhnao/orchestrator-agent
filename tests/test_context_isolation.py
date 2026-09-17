"""上下文隔离回归测试：并发请求不得互相串号。

背景（2026-09-16 改造）：
    tools.py 此前用模块级变量 `_active_trace_id` / `_active_token` / `_active_owner_id`
    承载请求身份，且 `/gateway` 端点定义为**同步** `def`（FastAPI 用线程池执行）
    ⇒ 同一进程内多个请求真并发时会互相覆盖，A 用户的凭证可能被 B 的请求带上。

    现改为 LangGraph 的 `context_schema + Runtime/ToolRuntime`：
    身份随单次调用注入，不经过模型、不落任何模块级状态。

本测试覆盖两条对外路径：同步（/gateway 走 run_supervisor_with_steps）
与流式（/chat/stream 走 stream_supervisor）。

⚠️ 测试写法注意（踩过一次）：
    `unittest.mock.patch` 修改的是**模块级属性**，多个线程各自 patch 会互相覆盖，
    从而把「测试脚手架自身的竞争」误报成「被测代码串号」。
    因此这里**只在主线程 patch 一次**，mock 内部按线程分派。
"""
import asyncio
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage  # noqa: E402

from supervisor import run_supervisor_with_steps, stream_supervisor  # noqa: E402
from tools import make_context  # noqa: E402


class FakeLLM:
    """按脚本返回预置消息；**按线程独立计数**，避免不同请求互相干扰脚本轮次。

    第一轮：决定调用 query_rag（触发下游 HTTP）
    第二轮：给出最终答复（结束）
    """

    def __init__(self):
        self._local = threading.local()

    def _next(self):
        n = getattr(self._local, "n", 0) + 1
        self._local.n = n
        if n == 1:
            return AIMessage(content="", tool_calls=[
                {"name": "query_rag", "args": {"question": "退款政策"}, "id": "c1",
                 "type": "tool_call"}
            ])
        return AIMessage(content="最终答复")

    def invoke(self, messages, **kwargs):
        return self._next()

    async def ainvoke(self, messages, **kwargs):
        return self._next()

    def bind_tools(self, tools, **kwargs):
        return self


class FakeResponse:
    status_code = 200

    def json(self):
        return {"answer": "政策内容占位"}


def _ctx(tag: str):
    return make_context(f"trace-{tag}", f"token-{tag}", f"owner-{tag}")


# 全局收集器：记录每次下游调用的 线程名 + 请求头
RECORDS: list = []
LOCK = threading.Lock()


def fake_post(url, json_data, headers=None):
    """替代 tools._http_post_with_retry：只记录请求头，不真发 HTTP。"""
    with LOCK:
        RECORDS.append({
            "thread": threading.current_thread().name,
            "headers": dict(headers or {}),
        })
    return FakeResponse()


def main_test() -> int:
    passed = failed = 0

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  ✅ {label}")
        else:
            failed += 1
            print(f"  ❌ {label}" + (f" —— {detail}" if detail else ""))

    # ⚠️ patch 只在主线程做一次，包住所有并发执行
    with patch("tools._http_post_with_retry", side_effect=fake_post), \
         patch("supervisor._get_llm", return_value=FakeLLM()):

        # ── 1. 同步路径并发 ──
        print("=" * 74)
        print("1) 同步路径（/gateway → run_supervisor_with_steps）：双用户并发")
        print("=" * 74)
        RECORDS.clear()
        out: dict = {}

        def worker(tag):
            out[tag] = run_supervisor_with_steps("退款要什么条件", _ctx(tag))

        ts = [threading.Thread(target=worker, args=(t,), name=t) for t in ("A", "B")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        check("两个请求都返回了结果", len(out) == 2, f"实际 {list(out)}")
        owners = [r["headers"].get("X-Owner-Id") for r in RECORDS]
        check("下游调用的 owner 头 A/B 各归各，没有串号",
              sorted(owners) == ["owner-A", "owner-B"],
              f"实际={owners}")
        traces = [r["headers"].get("X-Trace-Id") for r in RECORDS]
        check("下游调用的 trace 头没有串号",
              sorted(traces) == ["trace-A", "trace-B"],
              f"实际={traces}")
        tokens = [r["headers"].get("Authorization", "") for r in RECORDS]
        check("下游调用的凭证没有串号",
              sorted(tokens) == ["Bearer token-A", "Bearer token-B"],
              f"实际={tokens}")
        # 交叉校验：owner 与 trace 必须来自同一个请求
        pairs_ok = all(
            r["headers"].get("X-Owner-Id", "").replace("owner-", "")
            == r["headers"].get("X-Trace-Id", "").replace("trace-", "")
            for r in RECORDS
        )
        check("同一次下游调用里 trace 与 owner 属于同一请求", pairs_ok,
              f"实际={[(r['headers'].get('X-Trace-Id'), r['headers'].get('X-Owner-Id')) for r in RECORDS]}")

        # ── 2. 流式路径并发 ──
        print()
        print("=" * 74)
        print("2) 流式路径（/chat/stream → stream_supervisor）：双用户并发")
        print("=" * 74)
        RECORDS.clear()
        done: dict = {}

        async def run_stream(tag):
            events = []
            async for ev in stream_supervisor("退款要什么条件", _ctx(tag)):
                events.append(ev)
            return events

        def worker2(tag):
            done[tag] = asyncio.run(run_stream(tag))

        ts2 = [threading.Thread(target=worker2, args=(t,), name="sse-" + t) for t in ("A", "B")]
        for t in ts2:
            t.start()
        for t in ts2:
            t.join()

        check("两个流式请求都完成", len(done) == 2, f"实际 {list(done)}")
        for tag in ("A", "B"):
            evs = done.get(tag) or []
            check(f"流 {tag} 收到 done 事件",
                  any(e.get("type") == "done" for e in evs),
                  f"事件类型={[e.get('type') for e in evs]}")
        owners2 = [r["headers"].get("X-Owner-Id") for r in RECORDS]
        check("流式路径下游调用的 owner 没有串号",
              sorted(owners2) == ["owner-A", "owner-B"],
              f"实际={owners2}")

        # ── 3. 串行对照 ──
        print()
        print("=" * 74)
        print("3) 串行对照：单请求时身份必须完全正确")
        print("=" * 74)
        RECORDS.clear()
        run_supervisor_with_steps("退款要什么条件", _ctx("SOLO"))
        check("串行请求的 owner = owner-SOLO",
              all(r["headers"].get("X-Owner-Id") == "owner-SOLO" for r in RECORDS),
              f"实际={[r['headers'].get('X-Owner-Id') for r in RECORDS]}")
        check("串行请求的 trace = trace-SOLO",
              all(r["headers"].get("X-Trace-Id") == "trace-SOLO" for r in RECORDS),
              f"实际={[r['headers'].get('X-Trace-Id') for r in RECORDS]}")

    print()
    print("=" * 74)
    print(f"结果：{passed}/{passed + failed} 通过")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_test())
