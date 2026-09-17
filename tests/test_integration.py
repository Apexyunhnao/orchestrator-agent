"""
集成测试 — v2（LangGraph Supervisor 版）

背景（2026-09-13 重写）：
  原测试是 v1（HTTP 路由器）时代写的——测的是 `main.orchestrate` / `classify_intent` /
  `call_rag_agent`，这些函数在 v2 用 LangGraph 重构后已不存在，导致 4 个用例全部
  ImportError 失败。本文件按 v2 的真实结构重写。

三层验证（前两层全 mock，不碰网络/API，秒级）：
  Part 1  图逻辑：mock LLM 的决策脚本 + mock 子Agent HTTP → 断言工具调用路径、多轮、降级
  Part 2  HTTP 接口：FastAPI TestClient + mock → 断言输入校验、返回结构、日志落库、/stats、/health
  Part 3  真实全链路：调用 e2e_fullstack.py（需 DEEPSEEK_API_KEY + 两个子Agent在线）

跑法：
  python tests/test_integration.py          # Part 1 + Part 2（默认，秒级，无外部依赖）
  python tests/test_integration.py --e2e    # 额外跑 Part 3 真实全链路
  pytest tests/test_integration.py          # 兼容 pytest（若已安装）
"""

import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage  # noqa: E402


# ── 假响应 & 假 LLM ─────────────────────────────────────

class FakeResponse:
    """模拟 httpx.Response 的最小接口（工具层只用到 status_code / json()）。"""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data


MOCK_RAG_OK = {
    "question": "退货需要什么条件",
    "answer": "自签收之日起7天内可申请无理由退货，商品需保持完好",
    "sources": ["退货政策.md"],
    "elapsed_ms": 120,
}

MOCK_TICKET_OK = {
    "ticket_id": "API-TEST-001",
    "category": "订单",
    "status": "已处理",
    "resolution": "您的订单已发货，预计3天送达",
}


class FakeLLM:
    """按脚本顺序返回预设消息的假 LLM。

    每次 supervisor_node 调用 invoke() 时弹出下一条；
    想让它"调工具"就给一条带 tool_calls 的 AIMessage，想让它"结束"就给纯文本消息。
    """

    def __init__(self, script: list[AIMessage]):
        self.script = list(script)
        self.invoke_count = 0

    def invoke(self, messages, **kwargs):  # noqa: ARG002
        # P1 之后 supervisor 会传 config=（流式回调所需）；假 LLM 只需吞掉这个额外参数。
        # 断言强度不变 —— 仍然严格按脚本顺序返回预设消息。
        self.invoke_count += 1
        if self.script:
            return self.script.pop(0)
        return AIMessage(content="（测试脚本已用尽）")


def ai_tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    """构造一条"我要调工具"的 LLM 响应。"""
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def ai_text(text: str) -> AIMessage:
    """构造一条"我直接回答/结束"的 LLM 响应（无 tool_calls）。"""
    return AIMessage(content=text)


def _test_ctx(message_tag: str = "t"):
    """构造测试用请求上下文。

    本测试 mock 了 `tools._http_post_with_retry`（不打真实 HTTP），
    因此 token 不需要真的能验签通过；但上下文本身必须提供（图调用要求显式传入）。
    """
    from tools import make_context

    return make_context(f"test-{message_tag}-trace", "test-token", "test-owner")


def _run(script: list[AIMessage], http_return=None, http_side_effect=None, message: str = "退货需要什么条件"):
    """在 mock LLM + mock 子Agent HTTP 的前提下跑一次完整图，返回 (answer, tools_used)。"""
    from supervisor import run_supervisor_with_trace

    ctx = _test_ctx()
    fake = FakeLLM(script)
    with patch("supervisor._get_llm", return_value=fake):
        if http_side_effect is not None:
            with patch("tools._http_post_with_retry", side_effect=http_side_effect):
                return run_supervisor_with_trace(message, ctx)
        ret = http_return if http_return is not None else FakeResponse(MOCK_RAG_OK)
        with patch("tools._http_post_with_retry", return_value=ret):
            return run_supervisor_with_trace(message, ctx)


# ── Part 1：图逻辑（工具调用路径 / 多轮 / 降级）────────────

class TestSupervisorGraph:
    """验证 LangGraph 图的路由：LLM 决定调工具 → tools 节点 → 回到 supervisor → 结束。"""

    def test_query_rag_route(self):
        """只问政策 → 只调 query_rag，然后结束。"""
        answer, tools_used = _run([
            ai_tool_call("query_rag", {"question": "退货需要什么条件"}),
            ai_text("退货条件是签收后7天内、商品完好。"),
        ])
        assert tools_used == ["query_rag"], tools_used
        assert "7天" in answer

    def test_create_ticket_route(self):
        """只办业务 → 只调 create_ticket。"""
        answer, tools_used = _run(
            [
                ai_tool_call("create_ticket", {"message": "查一下ORD-1003的订单状态", "user_identifier": ""}),
                ai_text("您的订单已发货，预计3天送达。"),
            ],
            http_return=FakeResponse(MOCK_TICKET_OK),
            message="查一下ORD-1003的订单状态",
        )
        assert tools_used == ["create_ticket"], tools_used
        assert answer

    def test_multi_turn_mixed(self):
        """混合请求 → 先查政策再建工单（两轮工具，这正是 v2 相比 v1 的价值）。"""
        answer, tools_used = _run(
            [
                ai_tool_call("query_rag", {"question": "退货需要什么条件"}, "call_1"),
                ai_tool_call("create_ticket", {"message": "帮我把ORD-2001退了", "user_identifier": ""}, "call_2"),
                ai_text("已按7天无理由为您提交退货工单。"),
            ],
            http_return=FakeResponse(MOCK_RAG_OK),
            message="退货要什么条件，帮我把ORD-2001退了",
        )
        assert tools_used == ["query_rag", "create_ticket"], tools_used
        assert answer

    def test_direct_answer_without_tools(self):
        """闲聊/无关话题 → 不调工具也能正常结束，不崩。"""
        answer, tools_used = _run([ai_text("这个问题我帮不上忙，建议联系人工客服。")])
        assert tools_used == []
        assert answer

    def test_tool_failure_degrades_gracefully(self):
        """子Agent 不可用 → 工具返回可读降级文本，图不崩，仍能给出回答。"""
        import httpx

        answer, tools_used = _run(
            [
                ai_tool_call("query_rag", {"question": "退货需要什么条件"}),
                ai_text("知识库暂时不可用，请稍后再试。"),
            ],
            http_side_effect=httpx.ConnectError("connection refused"),
        )
        assert tools_used == ["query_rag"], tools_used
        assert answer  # 降级后仍有回答

    def test_extract_tools_used_dedupes(self):
        """同一工具被调两次 → 工具清单里去重（评估算法就靠这个）。"""
        answer, tools_used = _run([
            ai_tool_call("query_rag", {"question": "退货条件"}, "c1"),
            ai_tool_call("query_rag", {"question": "退款时限"}, "c2"),
            ai_text("都查到了。"),
        ])
        assert tools_used == ["query_rag"], tools_used


# ── Part 2：HTTP 接口（TestClient + mock）─────────────────

class TestGatewayAPI:
    """验证 FastAPI 层：输入校验、返回结构、日志落库、/stats、/health。"""

    def setup_method(self):
        """每个用例用独立的临时 SQLite，避免污染 data/gateway.db。"""
        # ignore_cleanup_errors：Windows 上 SQLite 文件句柄可能还没释放，删不掉就留着
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._db_patch = patch("main.DB_PATH", os.path.join(self._tmpdir.name, "test_gateway.db"))
        self._db_patch.start()

    def teardown_method(self):
        self._db_patch.stop()
        self._tmpdir.cleanup()

    def _client(self, login_as: str | None = "customer"):
        """取一个 TestClient，默认以 customer 登录。

        P1 之后 /gateway 与 /stats 都要求认证：未登录会先返回 401、根本走不到业务逻辑，
        所以业务断言必须先登录。传 login_as=None 得到未登录客户端，专门验证 401。
        """
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)
        if login_as:
            passwords = {"customer": "customer123", "agent": "agent123", "admin": "admin123"}
            resp = client.post("/auth/login",
                               json={"username": login_as, "password": passwords[login_as]})
            assert resp.status_code == 200, f"登录失败：{resp.status_code} {resp.text}"
        return client

    @staticmethod
    def _step(*tool_names: str) -> dict:
        """构造一条 LangGraph 执行步骤。

        /gateway 用 _collect_tools(steps) 从 step["items"][*]["tool_calls"][*]["name"]
        提取 tools_used，因此 mock 必须给真实 steps 结构，而不能给旧版的 ["工具名"] 列表。
        """
        return {"node": "tools", "items": [{"tool_calls": [{"name": n} for n in tool_names]}]}

    def test_gateway_returns_answer_and_tools(self):
        """/gateway 正常返回 {answer, tools_used}，并把这次请求写进日志。"""
        with patch("main.run_supervisor_with_steps",
                   return_value=("您的订单已发货。", [self._step("create_ticket")])):
            with self._client() as client:
                resp = client.post("/gateway", json={"message": "查一下ORD-1003的订单状态"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["answer"] == "您的订单已发货。"
        assert body["tools_used"] == ["create_ticket"]

    def test_gateway_rejects_empty_message(self):
        with self._client() as client:
            resp = client.post("/gateway", json={"message": "   "})
        assert resp.status_code == 400

    def test_gateway_rejects_too_long_message(self):
        with self._client() as client:
            resp = client.post("/gateway", json={"message": "退货" * 300})
        assert resp.status_code == 400

    def test_gateway_rejects_non_chinese(self):
        with self._client() as client:
            resp = client.post("/gateway", json={"message": "hello can you help me please"})
        assert resp.status_code == 400

    def test_stats_reports_tool_usage(self):
        """/stats 汇总请求总数和各工具被调次数（数据来自 SQLite）。

        /stats 要求 agent/admin 角色（客户访问运营数据是越权），因此这里以 agent 登录。
        """
        with patch("main.run_supervisor_with_steps", return_value=("好的", [self._step("query_rag")])):
            with self._client(login_as="agent") as client:
                client.post("/gateway", json={"message": "退货需要什么条件"})
                client.post("/gateway", json={"message": "退款要多久"})
                resp = client.get("/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_requests"] == 2
        assert body["tool_usage"].get("query_rag") == 2

    def test_health_aggregates_subservices(self):
        """/health 聚合三个服务；子服务不可达时 healthy=false 并标 unreachable。"""
        with patch("httpx.get", side_effect=Exception("boom")):
            with self._client() as client:
                resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["healthy"] is False
        assert body["services"]["orchestrator"] == "ok"
        assert body["services"]["rag"] == "unreachable"

    def test_gateway_requires_login(self):
        """未登录访问 /gateway → 401（统一入口必须认证，不能匿名调用）。"""
        with self._client(login_as=None) as client:
            resp = client.post("/gateway", json={"message": "退货需要什么条件"})
        assert resp.status_code == 401

    def test_stats_forbidden_for_customer(self):
        """客户访问 /stats（运营数据）→ 403 越权拦截。

        注意与未登录区分：已登录但角色不够是 403，未携带凭证才是 401。
        """
        with self._client(login_as="customer") as client:
            resp = client.get("/stats")
        assert resp.status_code == 403


# ── Part 3：真实全链路（手动，需 API key + 子Agent在线）────

def run_e2e():
    """调用 e2e_fullstack.py 做真实端到端验证（会真调 DeepSeek API）。"""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "e2e_fullstack.py")
    print("=" * 50)
    print("Part 3: 真实全链路（e2e_fullstack.py）")
    print("=" * 50)
    if not os.getenv("DEEPSEEK_API_KEY"):
        print("  跳过：未设置 DEEPSEEK_API_KEY")
        return False
    proc = subprocess.run([sys.executable, script], cwd=root, check=False)
    return proc.returncode == 0


# ── 直接运行（不依赖 pytest）──────────────────────────────

def _run_class(cls) -> tuple[int, int]:
    passed = failed = 0
    inst = cls()
    for name in sorted(m for m in dir(cls) if m.startswith("test_")):
        fn = getattr(inst, name)
        setup = getattr(inst, "setup_method", None)
        teardown = getattr(inst, "teardown_method", None)
        try:
            if setup:
                setup()
            fn()
            print(f"  PASS  {cls.__name__}.{name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {cls.__name__}.{name}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            if teardown:
                teardown()
    return passed, failed


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    total_passed = total_failed = 0
    for cls in (TestSupervisorGraph, TestGatewayAPI):
        print("=" * 50)
        print(f"{cls.__name__}")
        print("=" * 50)
        p, f = _run_class(cls)
        total_passed += p
        total_failed += f

    print(f"\n结果: {total_passed} passed, {total_failed} failed")

    if "--e2e" in sys.argv:
        run_e2e()
    else:
        print("Part 3 已跳过（加 --e2e 跑真实全链路）")

    sys.exit(1 if total_failed else 0)
