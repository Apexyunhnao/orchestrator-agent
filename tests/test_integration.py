"""
集成测试 — 验证编排器全链路逻辑。

第一部分：Mock 测试（不依赖子Agent在线）
第二部分：启动验证（需要子Agent服务在线）
"""

import sys
import os
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── 假数据 ────────────────────────────────────────────

MOCK_RAG_OK = {
    "question": "年假能休几天",
    "answer": "根据公司规定，年假为5-15天",
    "sources": ["员工手册.md"],
    "elapsed_ms": 234,
}

MOCK_TICKET_OK = {
    "ticket_id": "API-20240808-001",
    "category": "订单",
    "status": "已处理",
    "resolution": "您的订单已发货，预计3天送达",
    "escalate_reason": "",
}

MOCK_RAG_DOWN = {"error": "RAG服务暂不可用", "answer": "", "sources": []}
MOCK_TICKET_DOWN = {"error": "工单服务暂不可用", "ticket_id": "", "status": "不可用"}


# ── 第一部分：Mock 测试 ────────────────────────────────

class TestOrchestrationMock:
    """用 Mock 验证编排器的路由分发逻辑（不依赖子Agent在线）。"""

    def test_knowledge_route(self):
        """knowledge 意图 → 只调 RAG，不调 ticket。"""
        from main import orchestrate

        with patch("main.call_rag_agent", return_value=MOCK_RAG_OK) as mock_rag, \
             patch("main.call_service_agent") as mock_ticket, \
             patch("main.classify_intent", return_value="knowledge"):
            result = orchestrate("年假能休几天")

        assert result["intent"] == "knowledge"
        assert result["rag"] == MOCK_RAG_OK
        assert result["ticket"] is None
        mock_rag.assert_called_once_with("年假能休几天")
        mock_ticket.assert_not_called()
        assert len(result["summary"]) > 0

    def test_service_route(self):
        """service 意图 → 只调 ticket，不调 RAG。"""
        from main import orchestrate

        with patch("main.call_service_agent", return_value=MOCK_TICKET_OK) as mock_ticket, \
             patch("main.call_rag_agent") as mock_rag, \
             patch("main.classify_intent", return_value="service"):
            result = orchestrate("查一下ORD-1003")

        assert result["intent"] == "service"
        assert result["ticket"] == MOCK_TICKET_OK
        assert result["rag"] is None
        mock_ticket.assert_called_once_with("查一下ORD-1003", "")
        mock_rag.assert_not_called()
        assert len(result["summary"]) > 0

    def test_mixed_route(self):
        """mixed 意图 → RAG 和 ticket 都调。"""
        from main import orchestrate

        with patch("main.call_rag_agent", return_value=MOCK_RAG_OK) as mock_rag, \
             patch("main.call_service_agent", return_value=MOCK_TICKET_OK) as mock_ticket, \
             patch("main.classify_intent", return_value="mixed"):
            result = orchestrate("退货政策是什么，帮我把ORD-2001退了")

        assert result["intent"] == "mixed"
        assert result["rag"] == MOCK_RAG_OK
        assert result["ticket"] == MOCK_TICKET_OK
        mock_rag.assert_called_once()
        mock_ticket.assert_called_once()
        assert len(result["summary"]) > 0

    def test_degradation(self):
        """子Agent返回降级错误 → orchestrate 正常返回，不崩。"""
        from main import orchestrate

        with patch("main.call_rag_agent", return_value=MOCK_RAG_DOWN), \
             patch("main.call_service_agent") as mock_ticket, \
             patch("main.classify_intent", return_value="knowledge"):
            result = orchestrate("年假能休几天")

        assert result["intent"] == "knowledge"
        assert result["rag"] == MOCK_RAG_DOWN
        assert result["ticket"] is None
        assert len(result["summary"]) > 0  # 降级时 summary 不为空
        mock_ticket.assert_not_called()


# ── 第二部分：启动验证 ─────────────────────────────────

class TestEndToEnd:
    """启动真实服务做端到端验证。"""

    def test_full_stack(self):
        """启动 mock 子Agent + 编排器，验证全链路 HTTP 通信。"""
        import subprocess
        import httpx

        processes = []

        # RAG mock 服务
        rag_proc = subprocess.Popen(
            [sys.executable, "-c", RAG_MOCK_SERVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(("RAG", rag_proc))

        # Ticket mock 服务
        ticket_proc = subprocess.Popen(
            [sys.executable, "-c", TICKET_MOCK_SERVER],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(("Ticket", ticket_proc))

        # 编排器服务
        orch_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(("Orchestrator", orch_proc))

        try:
            # 等待服务就绪
            time.sleep(3)

            # 端到端请求
            resp = httpx.post(
                "http://127.0.0.1:8000/gateway",
                json={"message": "年假能休几天", "user_identifier": ""},
                timeout=15.0,
            )
            assert resp.status_code == 200, f"HTTP {resp.status_code}"
            data = resp.json()

            assert data["intent"] == "knowledge"
            assert data["rag"] is not None
            assert "answer" in data["rag"]
            assert len(data["summary"]) > 0

            print("[OK] end-to-end test passed")
            print(f"  intent={data['intent']}, answer={data['rag']['answer'][:30]}")

        finally:
            for name, proc in reversed(processes):
                proc.kill()
                proc.wait()


# ── Mock 服务脚本 ─────────────────────────────────────

RAG_MOCK_SERVER = """
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        resp = {"question": body.get("question",""), "answer": "年假5-15天", "sources": ["员工手册.md"], "elapsed_ms": 100}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', 8002), H).serve_forever()
"""

TICKET_MOCK_SERVER = """
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        resp = {"ticket_id": "API-TEST-001", "category": "订单", "status": "已处理", "resolution": "订单已发货"}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', 8001), H).serve_forever()
"""


# ── 直接运行 ──────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("Part 1: Mock tests")
    print("=" * 50)

    tests = TestOrchestrationMock()
    passed = 0
    failed = 0

    for name in sorted(m for m in dir(tests) if m.startswith("test_")):
        fn = getattr(tests, name)
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1

    print(f"\nMock: {passed} passed, {failed} failed")

    # Part 2 only if --e2e flag
    if "--e2e" in sys.argv:
        print("\n" + "=" * 50)
        print("Part 2: End-to-end startup test")
        print("=" * 50)
        TestEndToEnd().test_full_stack()
    else:
        print("\nPart 2: skipped (use --e2e to run)")
