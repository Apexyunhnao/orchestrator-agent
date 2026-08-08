"""
全链路端到端测试：mock RAG (:8002) + 客服工单 Agent (:8001) + 编排器。
验证 Supervisor → HTTP 工具调用 → 子Agent → 汇总回答 的完整链路。
"""

import json
import os
import subprocess
import sys
import time

# Windows GBK 终端兜底
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx

# ── Paths ─────────────────────────────────────────────

ORCH_ROOT = os.path.dirname(os.path.abspath(__file__))
TICKET_ROOT = r"E:\BCRJ\vsxiangmu\customer-service-agent"


# ── Mock RAG 服务 ─────────────────────────────────────

def start_mock_rag() -> subprocess.Popen:
    """启动 mock RAG Agent 在 8002 端口。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", MOCK_RAG_SCRIPT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


MOCK_RAG_SCRIPT = r"""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

POLICIES = {
    "退货": "自签收之日起7天内可申请无理由退货，商品需保持完好。退货运费由买家承担。",
    "退款": "退款在收到退货后1-3个工作日原路退回。质量问题退货运费由卖家承担。",
    "换货": "质量问题签收15天内可换货，1-3个工作日发出新品。",
    "物流": "一般地区3-5个工作日，偏远地区5-7个工作日。",
}

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        q = body.get("question", "")
        ans = "请参考公司售后政策。"
        for kw, text in POLICIES.items():
            if kw in q:
                ans = text; break
        resp = {"question": q, "answer": ans, "sources": ["售后政策.md"], "elapsed_ms": 50}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode())
    def log_message(self, *args): pass

HTTPServer(("127.0.0.1", 8002), H).serve_forever()
"""


# ── 主流程 ────────────────────────────────────────────

def test_case(msg: str, label: str) -> None:
    """测试单条消息的端到端链路。"""
    print(f"\n{'='*60}")
    print(f"  {label}: {msg}")
    print("=" * 60)

    # 直接调编排器核心函数（不走 FastAPI，但走完整 Supervisor → tools → HTTP 链路）
    sys.path.insert(0, ORCH_ROOT)
    from supervisor import run_supervisor_with_trace

    t0 = time.time()
    answer, tools_used = run_supervisor_with_trace(msg)
    elapsed = time.time() - t0

    print(f"  工具调用: {tools_used}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  回答: {answer[:300]}")
    print()


def main():
    processes = []

    try:
        # 1. 启动 mock RAG
        print("启动 mock RAG Agent (8002)...")
        rag_proc = start_mock_rag()
        processes.append(("RAG", rag_proc))

        # 2. 启动客服工单 Agent
        print("启动客服工单 Agent (8001)...")
        ticket_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8001"],
            cwd=TICKET_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        processes.append(("Ticket", ticket_proc))

        # 3. 等待服务就绪
        print("等待服务就绪 (5s)...")
        time.sleep(5)

        # 检查服务
        for name, port in [("RAG", 8002), ("Ticket", 8001)]:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/", timeout=3)
                print(f"  {name} ({port}): OK (HTTP {r.status_code})")
            except Exception as e:
                print(f"  {name} ({port}): UNREACHABLE ({e})")

        # 4. 跑测试
        test_case("退货需要什么条件", "纯政策查询 → 只调 RAG")
        test_case("帮我查一下 ORD-1003 的订单状态", "纯业务操作 → 只调 ticket")
        test_case("退货要什么条件，另外帮我把 ORD-1003 退了", "混合场景 → 先 RAG 后 ticket")

        print("=" * 60)
        print("  Full-stack e2e test completed")
        print("=" * 60)

    finally:
        print("\n关闭所有服务...")
        for name, proc in reversed(processes):
            proc.kill()
            proc.wait()
            print(f"  {name} 已关闭")


if __name__ == "__main__":
    main()
