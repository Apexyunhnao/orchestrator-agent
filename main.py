"""
FastAPI 入口 — Supervisor 编排器。
接收用户消息 → 调 Supervisor 多轮决策 → 返回汇总及工具调用记录。
每次请求写入 SQLite 日志，提供 /stats 统计接口。
"""

import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator

from supervisor import run_supervisor_with_trace

# ── 数据库初始化 ────────────────────────────────────────

DB_PATH = "data/gateway.db"


def _ensure_data_dir() -> None:
    """确保 data/ 目录存在。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def init_db() -> None:
    """建表（幂等）。"""
    _ensure_data_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gateway_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                tools_used TEXT NOT NULL,
                answer_summary TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()


def log_request(message: str, tools_used: list[str], answer: str, elapsed_ms: int) -> None:
    """写入一条网关请求日志。"""
    _ensure_data_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO gateway_logs (message, tools_used, answer_summary, elapsed_ms) VALUES (?, ?, ?, ?)",
            (message, json.dumps(tools_used, ensure_ascii=False), answer[:200], elapsed_ms),
        )
        conn.commit()


# ── 生命周期 ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建表。"""
    init_db()
    yield


app = FastAPI(title="智能客服 Supervisor", version="2.0.0", lifespan=lifespan)


# ── 请求/响应模型 ──────────────────────────────────────

class GatewayRequest(BaseModel):
    message: str
    user_identifier: str = ""

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        """输入校验：空消息/超长/非中文拒绝。"""
        if not v or not v.strip():
            raise HTTPException(status_code=400, detail="消息不能为空")
        if len(v) > 500:
            raise HTTPException(status_code=400, detail="消息超过500字，请精简后重试")
        # 计算中文字符占比
        chinese_chars = len(re.findall(r"[一-鿿]", v))
        if len(v.strip()) > 3 and chinese_chars / max(len(v.strip()), 1) < 0.3:
            raise HTTPException(status_code=400, detail="仅支持中文请求，请用中文描述您的问题")
        return v.strip()


class GatewayResponse(BaseModel):
    answer: str
    tools_used: list[str]


# ── 接口 ───────────────────────────────────────────────

@app.post("/gateway", response_model=GatewayResponse)
def gateway(req: GatewayRequest) -> dict[str, Any]:
    """统一入口：Supervisor 多轮决策 → 返回答案和工具调用记录。"""
    t_start = time.time()
    answer, tools_used = run_supervisor_with_trace(req.message)
    elapsed_ms = int((time.time() - t_start) * 1000)

    print(f"[main] {time.strftime('%H:%M:%S')} | tools={tools_used} | total={elapsed_ms}ms")

    # 写日志（不阻塞响应）
    try:
        log_request(req.message, tools_used, answer, elapsed_ms)
    except Exception as e:
        print(f"[main] 日志写入失败: {e}")

    return {"answer": answer, "tools_used": tools_used}


@app.get("/stats")
def stats() -> dict[str, Any]:
    """返回请求统计信息。"""
    _ensure_data_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 总请求数
        total = conn.execute("SELECT COUNT(*) as n FROM gateway_logs").fetchone()["n"]

        # 最近 10 条
        recent = [
            dict(row)
            for row in conn.execute(
                "SELECT id, message, tools_used, created_at FROM gateway_logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]

        # 各工具调用次数（JSON 反序列化统计）
        tool_rows = conn.execute("SELECT tools_used FROM gateway_logs").fetchall()
        tool_counts: dict[str, int] = {}
        for row in tool_rows:
            try:
                for name in json.loads(row["tools_used"]):
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

    return {
        "total_requests": total,
        "recent_10": recent,
        "tool_usage": tool_counts,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    """健康检查：编排器自身 + 子Agent 可达性。"""
    status = {"orchestrator": "ok", "rag": "unknown", "ticket": "unknown"}
    for name, url in [("rag", "http://127.0.0.1:8002/health"), ("ticket", "http://127.0.0.1:8001/health")]:
        try:
            r = httpx.get(url, timeout=3)
            status[name] = "ok" if r.status_code == 200 else f"error({r.status_code})"
        except Exception:
            status[name] = "unreachable"
    all_ok = all(v == "ok" for v in status.values())
    return {"healthy": all_ok, "services": status}


# ── Web 页面 ───────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _HTML_PAGE


_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智能客服 Supervisor</title>
<style>
  :root {
    --bg: #f5f5f5; --card-bg: #fff; --text: #333; --sub: #666;
    --border: #e0e0e0; --accent: #2563eb; --accent-hover: #1d4ed8;
    --radius: 10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); min-height: 100vh; display: flex; justify-content: center; padding: 40px 16px; }
  .container { width: 100%; max-width: 720px; }
  h1 { font-size: 22px; color: var(--text); margin-bottom: 6px; }
  .subtitle { font-size: 13px; color: var(--sub); margin-bottom: 24px; }
  .input-group { display: flex; gap: 8px; margin-bottom: 16px; }
  .input-group input { flex: 1; padding: 10px 14px; font-size: 15px; border: 1px solid var(--border); border-radius: var(--radius); outline: none; transition: border-color .2s; }
  .input-group input:focus { border-color: var(--accent); }
  .input-group button { padding: 10px 24px; font-size: 15px; background: var(--accent); color: #fff; border: none; border-radius: var(--radius); cursor: pointer; font-weight: 500; }
  .input-group button:hover { background: var(--accent-hover); }
  .input-group button:disabled { opacity: .5; cursor: not-allowed; }
  .result { display: none; }
  .result.show { display: block; }
  .loading { text-align: center; padding: 32px 0; color: var(--sub); }
  .loading .spinner { display: inline-block; width: 28px; height: 28px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .6s linear infinite; margin-bottom: 10px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .card { background: var(--card-bg); border-radius: var(--radius); padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  .answer { font-size: 15px; color: var(--text); line-height: 1.7; margin-bottom: 12px; }
  .meta { font-size: 12px; color: var(--sub); }
  .error-card { background: #fef2f2; border: 1px solid #fecaca; border-radius: var(--radius); padding: 16px 20px; color: #dc2626; }
</style>
</head>
<body>
<div class="container">
  <h1>🤖 智能客服 Supervisor</h1>
  <p class="subtitle">Supervisor 多轮决策 — 自动查政策、建工单，协调两个子Agent</p>
  <div class="input-group">
    <input id="msgInput" type="text" placeholder="输入你的问题…" autofocus />
    <button id="sendBtn" onclick="send()">发送</button>
  </div>
  <div id="result" class="result"></div>
</div>
<script>
const $ = id => document.getElementById(id);
async function send() {
  const input = $('msgInput'), btn = $('sendBtn'), msg = input.value.trim();
  if (!msg) return;
  const result = $('result');
  result.className = 'result show';
  result.innerHTML = '<div class="loading"><div class="spinner"></div><div>Supervisor 决策中…</div></div>';
  btn.disabled = true;
  try {
    const resp = await fetch('/gateway', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,user_identifier:''})});
    const data = await resp.json();
    result.innerHTML = `<div class="card"><div class="answer">${esc(data.answer)}</div><div class="meta">调用: ${data.tools_used?.join(', ') || '无'}</div></div>`;
  } catch(e) {
    result.innerHTML = `<div class="error-card">请求失败: ${e.message||'网络异常'}</div>`;
  } finally { btn.disabled = false; }
}
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
$('msgInput').addEventListener('keydown', e => { if (e.key==='Enter') send(); });
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
