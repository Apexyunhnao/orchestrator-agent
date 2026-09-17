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
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from auth import (
    COOKIE_NAME,
    DEMO_USERS,
    ROLE_LABELS,
    TOKEN_TTL_MINUTES,
    authenticate,
    create_access_token,
    current_user,
    require_roles,
)
from supervisor import run_supervisor_with_steps, run_supervisor_with_trace, stream_supervisor
from tools import make_context

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
        # 轻量迁移：老库补 steps 列（存完整执行链路，供 /ops 观测页回放）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(gateway_logs)")]
        if "steps" not in cols:
            conn.execute("ALTER TABLE gateway_logs ADD COLUMN steps TEXT NOT NULL DEFAULT '[]'")
        if "session_id" not in cols:
            conn.execute("ALTER TABLE gateway_logs ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
        # 轻量迁移：补 trace_id 列（跨服务关联标识，一次用户请求在三个服务里共用一个）
        if "trace_id" not in cols:
            conn.execute("ALTER TABLE gateway_logs ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
        conn.commit()


def log_request(
    message: str,
    tools_used: list[str],
    answer: str,
    elapsed_ms: int,
    steps: list[dict[str, Any]] | None = None,
    session_id: str = "",
    trace_id: str = "",
) -> None:
    """写入一条网关请求日志（含完整执行链路、会话标识与跨服务关联标识）。"""
    _ensure_data_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO gateway_logs (message, tools_used, answer_summary, elapsed_ms, steps, session_id, trace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message,
                json.dumps(tools_used, ensure_ascii=False),
                answer[:200],
                elapsed_ms,
                json.dumps(steps or [], ensure_ascii=False),
                session_id,
                trace_id,
            ),
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
    session_id: str = ""      # 会话标识：同一轮对话的多次请求共享，供观测页分组

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
    steps: list[dict[str, Any]] = []   # 单次请求的执行步骤（前端 Trace 面板用）
    elapsed_ms: int = 0                # 本次请求总耗时
    trace_id: str = ""                 # 跨服务关联标识（可用来在三个服务的日志里串链路）


# ── 认证接口 ───────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    """登录：校验账号密码 → 签发 JWT → 写入 HttpOnly Cookie。

    失败统一返回「账号或密码错误」，不用响应区分账号是否存在。
    """
    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="账号或密码错误")

    token = create_access_token(user)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,          # 前端 JS 读不到，降低 XSS 窃取面
        samesite="strict",      # 挡跨站携带，降低 CSRF 面
        secure=False,           # 演示走 http；生产必须 True
        max_age=TOKEN_TTL_MINUTES * 60,
        path="/",
    )
    print(f"[auth] 登录成功 user={user['username']} role={user['role']}")
    return {
        "username": user["username"],
        "role": user["role"],
        "role_label": ROLE_LABELS.get(user["role"], user["role"]),
        "display": user["display"],
        "expires_in_minutes": TOKEN_TTL_MINUTES,
    }


@app.post("/auth/logout")
def logout(response: Response) -> dict[str, Any]:
    """登出：清掉 Cookie。JWT 无状态，服务端不维护会话表。"""
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"success": True}


@app.get("/auth/me")
def whoami(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """返回当前登录用户，供前端判断是否需要显示登录页。"""
    return {
        "username": user["username"],
        "role": user["role"],
        "role_label": ROLE_LABELS.get(user["role"], user["role"]),
        "display": user["display"],
        "owner_id": user["owner_id"],
    }


# ── 接口 ───────────────────────────────────────────────

def _collect_tools(steps: list[dict[str, Any]]) -> list[str]:
    """从执行步骤中提取工具名列表，保持 tools_used 字段向后兼容。"""
    names: list[str] = []
    for step in steps:
        for item in step.get("items", []) or []:
            for tc in item.get("tool_calls", []) or []:
                n = tc.get("name", "")
                if n and n not in names:
                    names.append(n)
    return names


@app.post("/gateway", response_model=GatewayResponse)
def gateway(req: GatewayRequest, request: Request,
            user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """统一入口：Supervisor 多轮决策 → 返回答案、工具调用记录与执行步骤。"""
    # 关联标识 + 登录凭证：作为单次请求上下文注入图，由 runtime 直达工具（下游自行验签）
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    ctx = make_context(trace_id, user["token"], user.get("owner_id", ""))

    t_start = time.time()
    answer, steps = run_supervisor_with_steps(req.message, ctx)
    elapsed_ms = int((time.time() - t_start) * 1000)
    tools_used = _collect_tools(steps)

    print(f"[main] {time.strftime('%H:%M:%S')} | trace={trace_id} | tools={tools_used} | total={elapsed_ms}ms")

    # 写日志（不阻塞响应）
    try:
        log_request(req.message, tools_used, answer, elapsed_ms, steps, req.session_id, trace_id)
    except Exception as e:
        print(f"[main] 日志写入失败: {e}")

    return {"answer": answer, "tools_used": tools_used, "steps": steps,
            "elapsed_ms": elapsed_ms, "trace_id": trace_id}


@app.post("/chat/stream")
async def chat_stream(req: GatewayRequest, request: Request,
                      user: dict[str, Any] = Depends(current_user)) -> StreamingResponse:
    """流式入口：SSE 逐 token 推送最终回答，工具调用过程以下发状态事件。

    事件类型见 supervisor.stream_supervisor。日志在流结束后写入同一张 gateway_logs 表，
    因此 /ops 观测台对两种入口一视同仁。
    """
    # 关联标识 + 登录凭证：作为单次请求上下文注入图
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    ctx = make_context(trace_id, user["token"], user.get("owner_id", ""))
    t_start = time.time()

    async def gen():
        steps: list[dict[str, Any]] = []
        answer = ""
        tools_used: list[str] = []
        try:
            async for ev in stream_supervisor(req.message, ctx):
                if ev["type"] == "done":
                    steps = ev.get("steps") or []
                    answer = ev.get("answer") or ""
                    tools_used = ev.get("tools_used") or []
                    ev["trace_id"] = trace_id   # 前端可展示/复制，用于跨服务排查
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            err = {"type": "error", "message": f"{type(e).__name__}: {e}", "trace_id": trace_id}
            yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"

        elapsed_ms = int((time.time() - t_start) * 1000)
        print(f"[main] {time.strftime('%H:%M:%S')} | trace={trace_id} | 流式 tools={tools_used} | total={elapsed_ms}ms")
        try:
            log_request(req.message, tools_used, answer, elapsed_ms, steps, req.session_id, trace_id)
        except Exception as e:  # noqa: BLE001
            print(f"[main] 日志写入失败: {e}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",   # 防中间层缓冲导致"假流式"
            "Connection": "keep-alive",
        },
    )


@app.get("/stats")
def stats(_user: dict[str, Any] = Depends(require_roles("agent", "admin"))) -> dict[str, Any]:
    """返回请求统计信息。仅客服/管理员可看（客户不得访问运营数据）。"""
    _ensure_data_dir()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 总请求数
        total = conn.execute("SELECT COUNT(*) as n FROM gateway_logs").fetchone()["n"]

        # 最近 40 条（含完整链路与会话标识，供 /ops 观测页回放）
        recent = []
        for row in conn.execute(
            "SELECT id, message, tools_used, answer_summary, elapsed_ms, created_at, steps, session_id"
            " FROM gateway_logs ORDER BY id DESC LIMIT 40"
        ).fetchall():
            item = dict(row)
            try:
                item["steps"] = json.loads(item.get("steps") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["steps"] = []
            try:
                item["tools_used"] = json.loads(item.get("tools_used") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["tools_used"] = []
            recent.append(item)

        # 各工具调用次数（JSON 反序列化统计）
        tool_rows = conn.execute("SELECT tools_used FROM gateway_logs").fetchall()
        tool_counts: dict[str, int] = {}
        for row in tool_rows:
            try:
                for name in json.loads(row["tools_used"]):
                    tool_counts[name] = tool_counts.get(name, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

    avg_ms = 0
    if total:
        with sqlite3.connect(DB_PATH) as conn:
            avg_ms = conn.execute("SELECT AVG(elapsed_ms) FROM gateway_logs").fetchone()[0] or 0

    # 按会话聚合（观测页分组用）：一次 session = 多轮请求
    sessions: list[dict[str, Any]] = []
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT session_id, COUNT(*) AS turns, SUM(elapsed_ms) AS total_ms,"
            " MAX(created_at) AS last_at, MIN(id) AS first_id"
            " FROM gateway_logs WHERE session_id != '' GROUP BY session_id"
            " ORDER BY last_at DESC LIMIT 20"
        ).fetchall():
            item = dict(row)
            first = conn.execute(
                "SELECT message FROM gateway_logs WHERE id = ?", (item["first_id"],)
            ).fetchone()
            item["first_message"] = first["message"] if first else ""
            sessions.append(item)

    return {
        "total_requests": total,
        "avg_elapsed_ms": int(avg_ms),
        "recent": recent,
        "recent_10": recent[:10],       # 兼容旧字段
        "sessions": sessions,
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

# 演示页面禁用缓存，保证改动即时可见
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """客户端：客服对话界面（面向使用者，不暴露内部实现细节）。"""
    return HTMLResponse(_USER_PAGE, headers=_NO_CACHE)


@app.get("/ops", response_class=HTMLResponse)
def ops_console(_user: dict[str, Any] = Depends(require_roles("agent", "admin"))) -> HTMLResponse:
    """服务端观测台：请求链路 / 工具调用 / 耗时统计（面向客服与管理员）。

    客户角色访问会被拒（403），这是任务书第五章要求的权限边界之一。
    """
    return HTMLResponse(_OPS_PAGE, headers=_NO_CACHE)


_USER_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>客服对话 · 智能客服 Supervisor</title>
<style>
  :root {
    --bg:#F5F7FC; --surface:#FFFFFF; --sunken:#F7F9FD;
    --border:#E3E7F2; --border-strong:#CBD3E4; --hair:#EFF2F8;
    --text:#0F1520; --text-2:#3A4560; --text-3:#7A8BA0;
    --accent:#2563EB; --accent-hover:#1D4ED8; --accent-soft:#EEF3FF;
    --ok:#15803D; --ok-bg:#E7F6EC; --warn:#9A6700; --warn-bg:#FFF8E1;
    --err:#C1272D; --err-bg:#FDEBEC;
    --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Courier New", monospace;
    --s1:8px; --s2:12px; --s3:16px; --s4:24px; --s5:32px; --s6:48px;
    --radius:12px; --radius-sm:8px;
    --shadow-sm: 0 1px 2px rgba(20,45,110,.06), 0 0 0 1px rgba(20,45,110,.05);
    --shadow-md: 0 2px 4px rgba(20,45,110,.05), 0 8px 24px rgba(20,45,110,.10), 0 0 0 1px rgba(20,45,110,.06);
    --ease: cubic-bezier(.22,1,.36,1);
    --t-fast:140ms; --t-base:220ms; --t-slow:380ms;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    display:flex; overflow:hidden;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    font-size:14.5px; line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  .sidebar {
    width:248px; flex:none; background:var(--surface);
    border-right:1px solid var(--border);
    display:flex; flex-direction:column; padding:var(--s4) var(--s3); gap:var(--s5);
  }
  .brand { display:flex; align-items:center; gap:10px; padding:0 6px; }
  .brand-logo {
    width:32px; height:32px; flex:none; border-radius:9px;
    background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:800; font-size:14px; box-shadow:0 2px 8px rgba(37,99,235,.32);
  }
  .brand-name { font-size:13.5px; font-weight:700; letter-spacing:-.01em; line-height:1.3; }
  .brand-sub { font-size:11px; color:var(--text-3); }
  .nav { display:flex; flex-direction:column; gap:2px; }
  .nav-label {
    font-size:10.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--text-3); padding:0 8px; margin-bottom:6px;
  }
  .nav-item {
    display:flex; align-items:center; gap:10px;
    padding:9px 10px; border-radius:var(--radius-sm);
    color:var(--text-2); text-decoration:none; font-size:13.5px; font-weight:500;
    transition:background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
  }
  .nav-item:hover { background:var(--sunken); color:var(--text); }
  .nav-item.active { background:var(--accent-soft); color:var(--accent); font-weight:650; }
  .nav-item .ico { width:17px; text-align:center; font-size:13.5px; flex:none; opacity:.9; }
  .sidebar-foot { margin-top:auto; padding:0 8px; font-size:11px; color:var(--text-3); line-height:1.9; }
  .dot-ok { display:inline-block; width:6px; height:6px; border-radius:50%; background:#22C55E; margin-right:5px; vertical-align:1px; }

  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar {
    height:58px; flex:none; border-bottom:1px solid var(--border);
    background:rgba(255,255,255,.78); backdrop-filter:blur(10px);
    display:flex; align-items:center; justify-content:space-between; gap:var(--s3);
    padding:0 var(--s5);
  }
  .topbar h1 { font-size:15.5px; font-weight:700; letter-spacing:-.01em; }
  .topbar .sub { font-size:12.5px; color:var(--text-3); }
  .content { flex:1; overflow-y:auto; padding:var(--s5); }

  .btn-primary {
    padding:11px 26px; font-size:14.5px; font-family:inherit; font-weight:600;
    color:#fff; border:0; border-radius:var(--radius-sm); cursor:pointer; white-space:nowrap;
    background:linear-gradient(180deg,#3B76F0,#2563EB);
    box-shadow:0 1px 2px rgba(37,99,235,.30), inset 0 1px 0 rgba(255,255,255,.22);
    transition:filter var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
  }
  .btn-primary:hover:not(:disabled) { filter:brightness(1.07); }
  .btn-primary:active:not(:disabled) { transform:translateY(1px) scale(.995); box-shadow:inset 0 1px 3px rgba(0,0,0,.16); }
  .btn-primary:disabled { opacity:.45; cursor:not-allowed; }
  .chip {
    font-size:12.5px; font-family:inherit; color:var(--accent);
    background:var(--accent-soft); border:0; box-shadow:inset 0 0 0 1px rgba(37,99,235,.15);
    border-radius:999px; padding:5px 13px; cursor:pointer; text-align:left;
    transition:background var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
  }
  .chip:hover { background:#E3ECFF; box-shadow:inset 0 0 0 1px rgba(37,99,235,.28); }
  .chip:active { transform:scale(.97); }
  .badge { display:inline-flex; align-items:center; gap:4px; font-size:11.5px; font-weight:650; padding:2px 9px; border-radius:999px; }
  .badge-ok { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .badge-warn { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
  .badge-plain { color:var(--text-2); background:#F1F4FA; box-shadow:inset 0 0 0 1px var(--border); }
  .tag-tool {
    font-family:var(--mono); font-size:12px; background:#F1F4FA;
    box-shadow:inset 0 0 0 1px var(--border); border-radius:6px; padding:2px 8px; color:var(--text-2);
  }
  .arrow { color:var(--text-3); margin:0 5px; }
  .panel { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden; }
  .panel-head {
    padding:12px var(--s3); border-bottom:1px solid var(--hair);
    font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--text-3);
  }
  .empty { color:var(--text-3); font-size:13px; padding:var(--s4); text-align:center; }

  /* ══ 客户端：全宽对话（行业做法：不用左右气泡）══ */
  .thread { max-width:768px; margin:0 auto; }
  .turn { padding:var(--s4) 0; border-top:1px solid var(--hair); animation:rise var(--t-slow) var(--ease) both; }
  .turn:first-child { border-top:0; padding-top:0; }
  .turn-head { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .turn-badge {
    width:24px; height:24px; flex:none; border-radius:7px;
    display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700;
  }
  .turn.bot .turn-badge { background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff; box-shadow:0 2px 6px rgba(37,99,235,.26); }
  .turn.user .turn-badge { background:#E6ECF7; color:var(--text-2); }
  .turn-name { font-size:13px; font-weight:650; }
  .turn-time { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .turn-body { font-size:14.5px; line-height:1.72; white-space:pre-wrap; word-break:break-word; }
  .turn.user .turn-body { color:var(--text-2); }
  .turn.bot .turn-body.typing { color:var(--text-3); }
  .turn-meta { margin-top:10px; font-size:11.5px; color:var(--text-3); }
  .turn-meta a { color:var(--accent); text-decoration:none; font-weight:600; }
  .turn-meta a:hover { text-decoration:underline; }
  .composer {
    flex:none; border-top:1px solid var(--border);
    background:rgba(255,255,255,.86); backdrop-filter:blur(10px);
    padding:var(--s3) var(--s5) var(--s4);
  }
  .composer-inner { max-width:768px; margin:0 auto; }
  .composer-row { display:flex; gap:var(--s2); }
  .composer input[type=text] {
    flex:1; min-width:0; padding:12px 15px; font-size:14.5px; font-family:inherit;
    color:var(--text); background:#fff; border:0; border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border-strong); outline:none;
    transition:box-shadow var(--t-fast) var(--ease);
  }
  .composer input[type=text]:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
  .composer-hint { display:flex; flex-wrap:wrap; gap:var(--s1); align-items:center; margin-top:var(--s2); }
  .composer-hint .lbl { font-size:12px; color:var(--text-3); }

  /* ══ 服务端：观测台 ══ */
  .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:var(--s3); margin-bottom:var(--s3); }
  .metric { background:var(--surface); border-radius:var(--radius); padding:var(--s3) var(--s4); box-shadow:var(--shadow-sm); }
  .metric .lbl { font-size:11.5px; color:var(--text-3); margin-bottom:5px; font-weight:600; }
  .metric .val { font-size:25px; font-weight:750; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .metric .val.small { font-size:15px; font-weight:650; padding-top:6px; }
  .ops { display:grid; grid-template-columns:minmax(260px,340px) minmax(0,1fr); gap:var(--s3); align-items:start; }
  .list { max-height:calc(100vh - 300px); overflow-y:auto; }
  .row {
    padding:11px var(--s3); border-bottom:1px solid var(--hair); cursor:pointer;
    transition:background var(--t-fast) var(--ease);
  }
  .row:last-child { border-bottom:0; }
  .row:hover { background:var(--sunken); }
  .row.sel { background:var(--accent-soft); }
  .row-title { font-size:13px; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-meta { font-size:11px; color:var(--text-3); margin-top:3px; font-variant-numeric:tabular-nums; }
  .turn-card { border-bottom:1px solid var(--hair); }
  .turn-card:last-child { border-bottom:0; }
  .turn-card > summary {
    list-style:none; cursor:pointer; padding:var(--s3); display:block;
    transition:background var(--t-fast) var(--ease);
  }
  .turn-card > summary::-webkit-details-marker { display:none; }
  .turn-card > summary:hover { background:var(--sunken); }
  .turn-q { font-size:13.5px; font-weight:600; margin-bottom:5px; }
  .turn-sub { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .steps { padding:0 var(--s3) var(--s3); }
  .step { position:relative; padding-left:30px; padding-bottom:var(--s3); }
  .step:last-child { padding-bottom:2px; }
  .step::before { content:""; position:absolute; left:9px; top:24px; bottom:-2px; width:1px; background:var(--border); }
  .step:last-child::before { display:none; }
  .step-no {
    position:absolute; left:0; top:3px; width:20px; height:20px; border-radius:50%;
    background:var(--accent-soft); color:var(--accent); box-shadow:inset 0 0 0 1px rgba(37,99,235,.18);
    font-size:11px; font-weight:700; line-height:20px; text-align:center;
  }
  .step-head { display:flex; align-items:baseline; gap:var(--s2); flex-wrap:wrap; }
  .step-node { font-size:13.5px; font-weight:650; }
  .step-ms { font-size:12px; color:var(--accent); font-variant-numeric:tabular-nums; }
  .step-total { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .step-body { margin-top:var(--s2); display:flex; flex-direction:column; gap:var(--s2); }
  .item {
    background:var(--sunken); border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border); padding:var(--s2) var(--s3); font-size:12.5px;
  }
  .item-role { display:inline-block; font-size:11px; font-weight:700; color:var(--text-3); letter-spacing:.04em; margin-bottom:5px; }
  .call { display:flex; flex-direction:column; gap:4px; margin:4px 0; }
  .call-name { font-family:var(--mono); font-size:12.5px; font-weight:650; color:var(--accent); }
  .call-args {
    font-family:var(--mono); font-size:11.5px; color:var(--text-2); background:#fff;
    border-radius:6px; box-shadow:inset 0 0 0 1px var(--border);
    padding:6px 9px; white-space:pre-wrap; word-break:break-all;
  }
  .item-text {
    font-size:12.5px; color:var(--text-2); line-height:1.65; white-space:pre-wrap; word-break:break-word;
    max-height:160px; overflow-y:auto;
  }

  @keyframes rise { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
  }
  @media (max-width: 900px) {
    .sidebar { width:64px; padding:var(--s3) 10px; }
    .brand-text, .nav-item span:not(.ico), .nav-label, .sidebar-foot, .topbar .sub { display:none; }
    .ops { grid-template-columns:1fr; }
    .content { padding:var(--s3); }
    .topbar { padding:0 var(--s3); }
    .composer { padding:var(--s3); }
  }
  @media (max-width: 560px) {
    body { font-size:14px; }
    .composer-row { flex-direction:column; }
    .btn-primary { width:100%; }
    .composer-hint { flex-direction:column; align-items:stretch; }
  }
</style>
</head>
<body>
<aside class="sidebar">
  <div class="brand">
    <div class="brand-logo">S</div>
    <div class="brand-text">
      <div class="brand-name">智能客服 Supervisor</div>
      <div class="brand-sub">Multi-Agent Gateway</div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-label">工作台</div>
    <a class="nav-item active" href="/"><span class="ico">💬</span><span>客服对话</span></a>
    <a class="nav-item" href="/ops"><span class="ico">📊</span><span>运行观测</span></a>
  </nav>
  <div class="sidebar-foot">
    <div><span class="dot-ok"></span>服务正常</div>
    <div>build v5 · 全宽消息 + 会话分组</div>
  </div>
</aside>
<div class="main">
  <style>
    /* 登录遮罩（只属于客户端对话页） */
    .login-mask {
      position:fixed; inset:0; z-index:100; display:flex; align-items:center; justify-content:center;
      background:rgba(15,21,32,.42); backdrop-filter:blur(4px);
    }
    .login-mask[hidden] { display:none; }
    .login-card {
      width:352px; max-width:calc(100vw - 32px); background:var(--surface);
      border-radius:var(--radius); box-shadow:var(--shadow-md); padding:var(--s5);
      display:flex; flex-direction:column; gap:var(--s2);
    }
    .login-card h2 { font-size:17px; font-weight:750; letter-spacing:-.01em; }
    .login-sub { font-size:12px; color:var(--text-3); line-height:1.75; }
    .login-card input {
      height:42px; padding:0 13px; font-size:14px; font-family:inherit; color:var(--text);
      background:#fff; border:0; border-radius:var(--radius-sm);
      box-shadow:inset 0 0 0 1px var(--border-strong); outline:none;
      transition:box-shadow var(--t-fast) var(--ease);
    }
    .login-card input:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
    .login-err { min-height:17px; font-size:12.5px; color:var(--err); }
    .who { display:flex; align-items:center; gap:9px; font-size:12.5px; color:var(--text-2); }
    .who[hidden] { display:none; }
    .role-tag {
      font-size:11px; font-weight:700; padding:2px 7px; border-radius:999px;
      background:var(--accent-soft); color:var(--accent);
    }
    .btn-ghost {
      border:0; background:transparent; color:var(--text-3); font-size:12.5px; font-family:inherit;
      cursor:pointer; padding:5px 9px; border-radius:7px;
      transition:background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
    }
    .btn-ghost:hover { background:var(--sunken); color:var(--text); }
  </style>
  <div class="login-mask" id="loginMask" hidden>
    <div class="login-card">
      <h2>登录</h2>
      <p class="login-sub">演示账号：customer / agent / admin<br>密码见项目 README</p>
      <input id="loginUser" placeholder="账号" autocomplete="username">
      <input id="loginPass" type="password" placeholder="密码" autocomplete="current-password">
      <button class="btn-primary" id="loginBtn">登录</button>
      <div class="login-err" id="loginErr"></div>
    </div>
  </div>
  <div class="topbar">
    <h1>客服对话</h1>
    <div class="sub">Supervisor 编排 · 自动查政策 / 建工单</div>
    <div class="who" id="whoBox" hidden>
      <span id="whoName"></span>
      <span class="role-tag" id="whoRole"></span>
      <button class="btn-ghost" id="logoutBtn">退出</button>
    </div>
  </div>
  <div class="content" id="scroll">
    <div class="thread" id="thread"></div>
  </div>
  <div class="composer">
    <div class="composer-inner">
      <div class="composer-row">
        <input id="msgInput" type="text" placeholder="描述你的问题，例如：退货需要什么条件" autocomplete="off" />
        <button id="sendBtn" class="btn-primary" onclick="send()">发送</button>
      </div>
      <div class="composer-hint" id="hints">
        <span class="lbl">可能想问：</span>
      </div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const thread = $("thread");
const LS_HIST = "p1_chat_history";
const LS_SESS = "p1_session_id";
const WELCOME = "你好，我是客服助手。可以帮你查退货退款政策、查询订单状态、处理地址修改或售后问题，直接说需求就行。";

// 会话标识：同一轮对话共享，用于观测页把多轮请求串成一个 session
let SESSION_ID = "";
try {
  SESSION_ID = localStorage.getItem(LS_SESS) || "";
  if (!SESSION_ID) {
    SESSION_ID = "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
    localStorage.setItem(LS_SESS, SESSION_ID);
  }
} catch (e) { SESSION_ID = "s-" + Date.now().toString(36); }

let HISTORY = [];
try { const raw = JSON.parse(localStorage.getItem(LS_HIST) || "[]"); if (Array.isArray(raw)) HISTORY = raw; } catch (e) { HISTORY = []; }

const QUICK = [
  "退货需要什么条件",
  "退货要什么条件？帮我把 ORD-1003 退了",
  "ORD-1002 买的戴森是翻新机全是划痕，要求退款赔偿"
];
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
QUICK.forEach(q => {
  const b = document.createElement("button");
  b.type = "button"; b.className = "chip"; b.textContent = q; b.title = q;
  b.onclick = () => { $("msgInput").value = q; send(); };
  $("hints").appendChild(b);
});
function nowStr() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
function save() {
  try { localStorage.setItem(LS_HIST, JSON.stringify(HISTORY.slice(-60))); } catch (e) {}
}
function addTurn(role, text, meta, time, persist) {
  const el = document.createElement("article");
  el.className = "turn " + role;
  el.innerHTML =
    '<div class="turn-head">' +
      '<span class="turn-badge">' + (role === "user" ? "我" : "S") + '</span>' +
      '<span class="turn-name">' + (role === "user" ? "你" : "Supervisor") + '</span>' +
      '<span class="turn-time">' + esc(time || nowStr()) + '</span>' +
    '</div>' +
    '<div class="turn-body">' + esc(text) + '</div>' +
    (meta ? '<div class="turn-meta">' + meta + '</div>' : '');
  thread.appendChild(el);
  $("scroll").scrollTop = $("scroll").scrollHeight;
  if (persist !== false) {
    HISTORY.push({ role: role, text: text, meta: meta || "", time: time || nowStr() });
    save();
  }
  return el;
}
function restore() {
  thread.innerHTML = "";
  if (!HISTORY.length) { HISTORY.push({ role: "bot", text: WELCOME, meta: "", time: nowStr() }); save(); }
  HISTORY.forEach(m => addTurn(m.role, m.text, m.meta, m.time, false));
}
let ABORT = null;
function setBusy(on) {
  const b = $("sendBtn");
  b.textContent = on ? "停止" : "发送";
  b.onclick = on ? stopStream : send;
  b.style.background = on ? "#C1272D" : "";
  b.style.borderColor = on ? "#C1272D" : "";
}
function stopStream() {
  if (ABORT) { try { ABORT.abort(); } catch (e) {} }
}
async function send() {
  const msg = $("msgInput").value.trim();
  if (!msg) { $("msgInput").focus(); return; }
  addTurn("user", msg, "");
  $("msgInput").value = "";
  setBusy(true);

  const el = addTurn("bot", "正在为你查询…", "", nowStr(), false);
  const body = el.querySelector(".turn-body");
  body.classList.add("typing");

  let text = "", meta = "", streaming = false;
  const t0 = Date.now();
  ABORT = new AbortController();
  try {
    const resp = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, user_identifier: "", session_id: SESSION_ID }),
      signal: ABORT.signal
    });
    if (resp.status === 401) {
      el.remove();
      setBusy(false);
      ABORT = null;
      showLogin("登录已过期，请重新登录");
      return;
    }
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const r = await reader.read();
      if (r.done) break;
      buf += dec.decode(r.value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop();
      for (const f of frames) {
        const line = f.split("\n").find(l => l.indexOf("data: ") === 0);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }

        if (ev.type === "status") {
          if (!streaming) body.textContent = "正在调用 " + ev.detail + " …";
        } else if (ev.type === "token") {
          if (!streaming) { streaming = true; text = ""; body.classList.remove("typing"); }
          text += ev.text;
          body.textContent = text;
          $("scroll").scrollTop = $("scroll").scrollHeight;
        } else if (ev.type === "revoke") {
          text = ""; streaming = false;
          body.textContent = ""; body.classList.add("typing");
        } else if (ev.type === "done") {
          if (ev.answer) text = ev.answer;
          const tools = (ev.tools_used || []).length;
          meta = '<a href="/ops">查看执行详情 →</a>　·　' +
            ((ev.elapsed_ms || (Date.now() - t0)) / 1000).toFixed(1) + "s" +
            (tools ? "　·　调用 " + tools + " 个工具" : "");
        } else if (ev.type === "error") {
          streaming = false; body.classList.remove("typing");
          body.textContent = "抱歉，处理失败：" + ev.message;
        }
      }
    }
  } catch (e) {
    if (!(e && e.name === "AbortError")) {
      streaming = false; body.classList.remove("typing");
      body.textContent = "网络或服务异常：" + (e && e.message ? e.message : e);
    }
  }

  body.classList.remove("typing");
  if (streaming && text) body.textContent = text;
  else if (!body.textContent) body.textContent = "（已停止）";

  const finalText = body.textContent || "";
  if (finalText) {
    if (meta) {
      const m = document.createElement("div");
      m.className = "turn-meta";
      m.innerHTML = meta;
      el.appendChild(m);
    }
    HISTORY.push({ role: "bot", text: finalText, meta: meta, time: nowStr() });
    save();
  }
  setBusy(false);
  ABORT = null;
  $("msgInput").focus();
}
function clearChat() {
  HISTORY = [];
  SESSION_ID = "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
  try { localStorage.removeItem(LS_HIST); localStorage.setItem(LS_SESS, SESSION_ID); } catch (e) {}
  restore();
}
$("msgInput").addEventListener("keydown", e => { if (e.key === "Enter") send(); });

// ── 登录态（JWT 存在 HttpOnly Cookie 里，前端读不到，只能问“我是谁”）──
let ME = null;
function showLogin(msg) {
  $("loginErr").textContent = msg || "";
  $("loginMask").hidden = false;
  $("whoBox").hidden = true;
  setTimeout(() => $("loginUser").focus(), 30);
}
function hideLogin() { $("loginMask").hidden = true; }
function renderUser() {
  if (!ME) { $("whoBox").hidden = true; return; }
  $("whoName").textContent = ME.display || ME.username;
  $("whoRole").textContent = ME.role_label || ME.role;
  $("whoBox").hidden = false;
}
async function doLogin() {
  const u = $("loginUser").value.trim(), p = $("loginPass").value;
  if (!u || !p) { $("loginErr").textContent = "请输入账号和密码"; return; }
  $("loginBtn").disabled = true;
  $("loginErr").textContent = "";
  try {
    const r = await fetch("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p })
    });
    if (!r.ok) {
      let d = {};
      try { d = await r.json(); } catch (e) {}
      $("loginErr").textContent = d.detail || ("登录失败（HTTP " + r.status + "）");
      return;
    }
    ME = await r.json();
    $("loginPass").value = "";
    hideLogin();
    renderUser();
  } catch (e) {
    $("loginErr").textContent = "网络异常：" + (e && e.message ? e.message : e);
  } finally {
    $("loginBtn").disabled = false;
  }
}
async function doLogout() {
  try { await fetch("/auth/logout", { method: "POST" }); } catch (e) {}
  ME = null;
  renderUser();
  showLogin("已退出登录");
}
async function bootAuth() {
  try {
    const r = await fetch("/auth/me");
    if (r.ok) { ME = await r.json(); hideLogin(); renderUser(); }
    else { ME = null; showLogin("请先登录后使用"); }
  } catch (e) {
    showLogin("无法连接服务");
  }
}
$("loginBtn").addEventListener("click", doLogin);
$("loginPass").addEventListener("keydown", e => { if (e.key === "Enter") doLogin(); });
$("loginUser").addEventListener("keydown", e => { if (e.key === "Enter") $("loginPass").focus(); });
$("logoutBtn").addEventListener("click", doLogout);

restore();
bootAuth();
</script>
</body>
</html>"""


_OPS_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>运行观测 · 智能客服 Supervisor</title>
<style>
  :root {
    --bg:#F5F7FC; --surface:#FFFFFF; --sunken:#F7F9FD;
    --border:#E3E7F2; --border-strong:#CBD3E4; --hair:#EFF2F8;
    --text:#0F1520; --text-2:#3A4560; --text-3:#7A8BA0;
    --accent:#2563EB; --accent-hover:#1D4ED8; --accent-soft:#EEF3FF;
    --ok:#15803D; --ok-bg:#E7F6EC; --warn:#9A6700; --warn-bg:#FFF8E1;
    --err:#C1272D; --err-bg:#FDEBEC;
    --mono: ui-monospace, SFMono-Regular, "Cascadia Mono", Consolas, "Courier New", monospace;
    --s1:8px; --s2:12px; --s3:16px; --s4:24px; --s5:32px; --s6:48px;
    --radius:12px; --radius-sm:8px;
    --shadow-sm: 0 1px 2px rgba(20,45,110,.06), 0 0 0 1px rgba(20,45,110,.05);
    --shadow-md: 0 2px 4px rgba(20,45,110,.05), 0 8px 24px rgba(20,45,110,.10), 0 0 0 1px rgba(20,45,110,.06);
    --ease: cubic-bezier(.22,1,.36,1);
    --t-fast:140ms; --t-base:220ms; --t-slow:380ms;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  html, body { height:100%; }
  body {
    display:flex; overflow:hidden;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI Variable Display","Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
    font-size:14.5px; line-height:1.6; -webkit-font-smoothing:antialiased;
  }
  .sidebar {
    width:248px; flex:none; background:var(--surface);
    border-right:1px solid var(--border);
    display:flex; flex-direction:column; padding:var(--s4) var(--s3); gap:var(--s5);
  }
  .brand { display:flex; align-items:center; gap:10px; padding:0 6px; }
  .brand-logo {
    width:32px; height:32px; flex:none; border-radius:9px;
    background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff;
    display:flex; align-items:center; justify-content:center;
    font-weight:800; font-size:14px; box-shadow:0 2px 8px rgba(37,99,235,.32);
  }
  .brand-name { font-size:13.5px; font-weight:700; letter-spacing:-.01em; line-height:1.3; }
  .brand-sub { font-size:11px; color:var(--text-3); }
  .nav { display:flex; flex-direction:column; gap:2px; }
  .nav-label {
    font-size:10.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--text-3); padding:0 8px; margin-bottom:6px;
  }
  .nav-item {
    display:flex; align-items:center; gap:10px;
    padding:9px 10px; border-radius:var(--radius-sm);
    color:var(--text-2); text-decoration:none; font-size:13.5px; font-weight:500;
    transition:background var(--t-fast) var(--ease), color var(--t-fast) var(--ease);
  }
  .nav-item:hover { background:var(--sunken); color:var(--text); }
  .nav-item.active { background:var(--accent-soft); color:var(--accent); font-weight:650; }
  .nav-item .ico { width:17px; text-align:center; font-size:13.5px; flex:none; opacity:.9; }
  .sidebar-foot { margin-top:auto; padding:0 8px; font-size:11px; color:var(--text-3); line-height:1.9; }
  .dot-ok { display:inline-block; width:6px; height:6px; border-radius:50%; background:#22C55E; margin-right:5px; vertical-align:1px; }

  .main { flex:1; min-width:0; display:flex; flex-direction:column; }
  .topbar {
    height:58px; flex:none; border-bottom:1px solid var(--border);
    background:rgba(255,255,255,.78); backdrop-filter:blur(10px);
    display:flex; align-items:center; justify-content:space-between; gap:var(--s3);
    padding:0 var(--s5);
  }
  .topbar h1 { font-size:15.5px; font-weight:700; letter-spacing:-.01em; }
  .topbar .sub { font-size:12.5px; color:var(--text-3); }
  .content { flex:1; overflow-y:auto; padding:var(--s5); }

  .btn-primary {
    padding:11px 26px; font-size:14.5px; font-family:inherit; font-weight:600;
    color:#fff; border:0; border-radius:var(--radius-sm); cursor:pointer; white-space:nowrap;
    background:linear-gradient(180deg,#3B76F0,#2563EB);
    box-shadow:0 1px 2px rgba(37,99,235,.30), inset 0 1px 0 rgba(255,255,255,.22);
    transition:filter var(--t-fast) var(--ease), transform var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease);
  }
  .btn-primary:hover:not(:disabled) { filter:brightness(1.07); }
  .btn-primary:active:not(:disabled) { transform:translateY(1px) scale(.995); box-shadow:inset 0 1px 3px rgba(0,0,0,.16); }
  .btn-primary:disabled { opacity:.45; cursor:not-allowed; }
  .chip {
    font-size:12.5px; font-family:inherit; color:var(--accent);
    background:var(--accent-soft); border:0; box-shadow:inset 0 0 0 1px rgba(37,99,235,.15);
    border-radius:999px; padding:5px 13px; cursor:pointer; text-align:left;
    transition:background var(--t-fast) var(--ease), box-shadow var(--t-fast) var(--ease), transform var(--t-fast) var(--ease);
  }
  .chip:hover { background:#E3ECFF; box-shadow:inset 0 0 0 1px rgba(37,99,235,.28); }
  .chip:active { transform:scale(.97); }
  .badge { display:inline-flex; align-items:center; gap:4px; font-size:11.5px; font-weight:650; padding:2px 9px; border-radius:999px; }
  .badge-ok { color:var(--ok); background:var(--ok-bg); box-shadow:inset 0 0 0 1px rgba(21,128,61,.16); }
  .badge-warn { color:var(--warn); background:var(--warn-bg); box-shadow:inset 0 0 0 1px rgba(154,103,0,.16); }
  .badge-plain { color:var(--text-2); background:#F1F4FA; box-shadow:inset 0 0 0 1px var(--border); }
  .tag-tool {
    font-family:var(--mono); font-size:12px; background:#F1F4FA;
    box-shadow:inset 0 0 0 1px var(--border); border-radius:6px; padding:2px 8px; color:var(--text-2);
  }
  .arrow { color:var(--text-3); margin:0 5px; }
  .panel { background:var(--surface); border-radius:var(--radius); box-shadow:var(--shadow-sm); overflow:hidden; }
  .panel-head {
    padding:12px var(--s3); border-bottom:1px solid var(--hair);
    font-size:11px; font-weight:700; letter-spacing:.07em; text-transform:uppercase; color:var(--text-3);
  }
  .empty { color:var(--text-3); font-size:13px; padding:var(--s4); text-align:center; }

  /* ══ 客户端：全宽对话（行业做法：不用左右气泡）══ */
  .thread { max-width:768px; margin:0 auto; }
  .turn { padding:var(--s4) 0; border-top:1px solid var(--hair); animation:rise var(--t-slow) var(--ease) both; }
  .turn:first-child { border-top:0; padding-top:0; }
  .turn-head { display:flex; align-items:center; gap:8px; margin-bottom:10px; }
  .turn-badge {
    width:24px; height:24px; flex:none; border-radius:7px;
    display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:700;
  }
  .turn.bot .turn-badge { background:linear-gradient(135deg,#2563EB,#60A5FA); color:#fff; box-shadow:0 2px 6px rgba(37,99,235,.26); }
  .turn.user .turn-badge { background:#E6ECF7; color:var(--text-2); }
  .turn-name { font-size:13px; font-weight:650; }
  .turn-time { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .turn-body { font-size:14.5px; line-height:1.72; white-space:pre-wrap; word-break:break-word; }
  .turn.user .turn-body { color:var(--text-2); }
  .turn.bot .turn-body.typing { color:var(--text-3); }
  .turn-meta { margin-top:10px; font-size:11.5px; color:var(--text-3); }
  .turn-meta a { color:var(--accent); text-decoration:none; font-weight:600; }
  .turn-meta a:hover { text-decoration:underline; }
  .composer {
    flex:none; border-top:1px solid var(--border);
    background:rgba(255,255,255,.86); backdrop-filter:blur(10px);
    padding:var(--s3) var(--s5) var(--s4);
  }
  .composer-inner { max-width:768px; margin:0 auto; }
  .composer-row { display:flex; gap:var(--s2); }
  .composer input[type=text] {
    flex:1; min-width:0; padding:12px 15px; font-size:14.5px; font-family:inherit;
    color:var(--text); background:#fff; border:0; border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border-strong); outline:none;
    transition:box-shadow var(--t-fast) var(--ease);
  }
  .composer input[type=text]:focus { box-shadow:inset 0 0 0 1px var(--accent), 0 0 0 4px rgba(37,99,235,.13); }
  .composer-hint { display:flex; flex-wrap:wrap; gap:var(--s1); align-items:center; margin-top:var(--s2); }
  .composer-hint .lbl { font-size:12px; color:var(--text-3); }

  /* ══ 服务端：观测台 ══ */
  .metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:var(--s3); margin-bottom:var(--s3); }
  .metric { background:var(--surface); border-radius:var(--radius); padding:var(--s3) var(--s4); box-shadow:var(--shadow-sm); }
  .metric .lbl { font-size:11.5px; color:var(--text-3); margin-bottom:5px; font-weight:600; }
  .metric .val { font-size:25px; font-weight:750; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .metric .val.small { font-size:15px; font-weight:650; padding-top:6px; }
  .ops { display:grid; grid-template-columns:minmax(260px,340px) minmax(0,1fr); gap:var(--s3); align-items:start; }
  .list { max-height:calc(100vh - 300px); overflow-y:auto; }
  .row {
    padding:11px var(--s3); border-bottom:1px solid var(--hair); cursor:pointer;
    transition:background var(--t-fast) var(--ease);
  }
  .row:last-child { border-bottom:0; }
  .row:hover { background:var(--sunken); }
  .row.sel { background:var(--accent-soft); }
  .row-title { font-size:13px; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .row-meta { font-size:11px; color:var(--text-3); margin-top:3px; font-variant-numeric:tabular-nums; }
  .turn-card { border-bottom:1px solid var(--hair); }
  .turn-card:last-child { border-bottom:0; }
  .turn-card > summary {
    list-style:none; cursor:pointer; padding:var(--s3); display:block;
    transition:background var(--t-fast) var(--ease);
  }
  .turn-card > summary::-webkit-details-marker { display:none; }
  .turn-card > summary:hover { background:var(--sunken); }
  .turn-q { font-size:13.5px; font-weight:600; margin-bottom:5px; }
  .turn-sub { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .steps { padding:0 var(--s3) var(--s3); }
  .step { position:relative; padding-left:30px; padding-bottom:var(--s3); }
  .step:last-child { padding-bottom:2px; }
  .step::before { content:""; position:absolute; left:9px; top:24px; bottom:-2px; width:1px; background:var(--border); }
  .step:last-child::before { display:none; }
  .step-no {
    position:absolute; left:0; top:3px; width:20px; height:20px; border-radius:50%;
    background:var(--accent-soft); color:var(--accent); box-shadow:inset 0 0 0 1px rgba(37,99,235,.18);
    font-size:11px; font-weight:700; line-height:20px; text-align:center;
  }
  .step-head { display:flex; align-items:baseline; gap:var(--s2); flex-wrap:wrap; }
  .step-node { font-size:13.5px; font-weight:650; }
  .step-ms { font-size:12px; color:var(--accent); font-variant-numeric:tabular-nums; }
  .step-total { font-size:11.5px; color:var(--text-3); font-variant-numeric:tabular-nums; }
  .step-body { margin-top:var(--s2); display:flex; flex-direction:column; gap:var(--s2); }
  .item {
    background:var(--sunken); border-radius:var(--radius-sm);
    box-shadow:inset 0 0 0 1px var(--border); padding:var(--s2) var(--s3); font-size:12.5px;
  }
  .item-role { display:inline-block; font-size:11px; font-weight:700; color:var(--text-3); letter-spacing:.04em; margin-bottom:5px; }
  .call { display:flex; flex-direction:column; gap:4px; margin:4px 0; }
  .call-name { font-family:var(--mono); font-size:12.5px; font-weight:650; color:var(--accent); }
  .call-args {
    font-family:var(--mono); font-size:11.5px; color:var(--text-2); background:#fff;
    border-radius:6px; box-shadow:inset 0 0 0 1px var(--border);
    padding:6px 9px; white-space:pre-wrap; word-break:break-all;
  }
  .item-text {
    font-size:12.5px; color:var(--text-2); line-height:1.65; white-space:pre-wrap; word-break:break-word;
    max-height:160px; overflow-y:auto;
  }

  @keyframes rise { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:none; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation:none !important; transition:none !important; }
  }
  @media (max-width: 900px) {
    .sidebar { width:64px; padding:var(--s3) 10px; }
    .brand-text, .nav-item span:not(.ico), .nav-label, .sidebar-foot, .topbar .sub { display:none; }
    .ops { grid-template-columns:1fr; }
    .content { padding:var(--s3); }
    .topbar { padding:0 var(--s3); }
    .composer { padding:var(--s3); }
  }
  @media (max-width: 560px) {
    body { font-size:14px; }
    .composer-row { flex-direction:column; }
    .btn-primary { width:100%; }
    .composer-hint { flex-direction:column; align-items:stretch; }
  }
</style>
</head>
<body>
<aside class="sidebar">
  <div class="brand">
    <div class="brand-logo">S</div>
    <div class="brand-text">
      <div class="brand-name">智能客服 Supervisor</div>
      <div class="brand-sub">Multi-Agent Gateway</div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-label">工作台</div>
    <a class="nav-item" href="/"><span class="ico">💬</span><span>客服对话</span></a>
    <a class="nav-item active" href="/ops"><span class="ico">📊</span><span>运行观测</span></a>
  </nav>
  <div class="sidebar-foot">
    <div><span class="dot-ok"></span>服务正常</div>
    <div>build v5 · 全宽消息 + 会话分组</div>
  </div>
</aside>
<div class="main">
  <div class="topbar">
    <h1>运行观测</h1>
    <div class="sub">会话 · 请求链路 · 工具调用 · 耗时</div>
  </div>
  <div class="content">
    <div class="metrics" id="metrics"></div>
    <div class="ops">
      <div class="panel">
        <div class="panel-head">会话列表</div>
        <div class="list" id="sessList"><div class="empty">加载中…</div></div>
      </div>
      <div class="panel">
        <div class="panel-head">会话详情</div>
        <div id="detail"><div class="empty">从左侧选择一个会话，查看它的多轮对话与每轮执行链路</div></div>
      </div>
    </div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const NODE_LABEL = { supervisor: "Supervisor 决策", tools: "工具执行" };
let STATS = null, SEL_SESS = null;
let METRIC_SIG = "", LIST_SIG = "", DETAIL_SIG = "";

function sigList(d) {
  return JSON.stringify((d.sessions || []).map(s => [s.session_id, s.turns, s.last_at]));
}
function sigDetail(d) {
  const recs = (d.recent || []).filter(r => r.session_id === SEL_SESS);
  return String(SEL_SESS) + "|" + JSON.stringify(recs.map(r => [r.id, r.elapsed_ms]));
}

function renderMetrics() {
  const s = STATS || {};
  const tools = s.tool_usage || {};
  const toolStr = Object.keys(tools).length
    ? Object.entries(tools).map(([k, v]) => k + " ×" + v).join("　") : "—";
  const items = [
    { lbl: "累计请求", val: (s.total_requests || 0) },
    { lbl: "会话数", val: (s.sessions || []).length },
    { lbl: "平均耗时", val: s.avg_elapsed_ms ? (s.avg_elapsed_ms / 1000).toFixed(1) + "s" : "—" },
    { lbl: "工具调用分布", val: toolStr, small: true }
  ];
  $("metrics").innerHTML = items.map(i =>
    '<div class="metric"><div class="lbl">' + esc(i.lbl) + '</div>' +
    '<div class="val' + (i.small ? " small" : "") + '">' + esc(i.val) + '</div></div>').join("");
}

function renderList() {
  const list = (STATS && STATS.sessions) || [];
  if (!list.length) {
    $("sessList").innerHTML = '<div class="empty">还没有会话记录<br/>去「客服对话」发一条就会有</div>';
    return;
  }
  $("sessList").innerHTML = list.map(s =>
    '<div class="row' + (s.session_id === SEL_SESS ? " sel" : "") + '" data-sid="' + esc(s.session_id) + '">' +
      '<div class="row-title">' + esc(s.first_message || "(空)") + '</div>' +
      '<div class="row-meta">' + esc(s.session_id.slice(0, 14)) + '　·　' +
        s.turns + ' 轮　·　' + (s.total_ms / 1000).toFixed(1) + 's　·　' + esc(s.last_at) + '</div>' +
    '</div>').join("");
  Array.from($("sessList").children).forEach(el => {
    el.onclick = () => {
      SEL_SESS = el.dataset.sid;
      DETAIL_SIG = sigDetail(STATS);
      renderList(); renderDetail();
    };
  });
}

function renderItem(it) {
  const role = it.type === "ToolMessage"
    ? "工具返回" + (it.name ? " · " + it.name : "")
    : ((it.tool_calls && it.tool_calls.length) ? "决策：调用工具" : "模型输出");
  let inner = "";
  if (it.tool_calls && it.tool_calls.length) {
    inner += it.tool_calls.map(tc =>
      '<div class="call"><div class="call-name">' + esc(tc.name) + '</div>' +
      '<div class="call-args">' + esc(JSON.stringify(tc.args, null, 2)) + '</div></div>').join("");
  }
  if (it.content) inner += '<div class="item-text">' + esc(it.content) + '</div>';
  if (!inner) inner = '<div class="empty">（无内容）</div>';
  return '<div class="item"><div class="item-role">' + esc(role) + '</div>' + inner + '</div>';
}

function renderSteps(steps) {
  if (!steps || !steps.length) return '<div class="empty">这一轮没有链路数据（迁移前的历史日志）</div>';
  return '<div class="steps">' + steps.map((s, i) => {
    const items = (s.items || []).map(renderItem).join("") || '<div class="empty">（无内容）</div>';
    return '<div class="step">' +
      '<span class="step-no">' + (i + 1) + '</span>' +
      '<div class="step-head"><span class="step-node">' + esc(NODE_LABEL[s.node] || s.node) + '</span>' +
        '<span class="step-ms">' + (s.step_ms / 1000).toFixed(1) + 's</span>' +
        '<span class="step-total">累计 ' + (s.total_ms / 1000).toFixed(1) + 's</span></div>' +
      '<div class="step-body">' + items + '</div></div>';
  }).join("") + '</div>';
}

function renderDetail() {
  const recs = ((STATS && STATS.recent) || []).filter(r => r.session_id === SEL_SESS);
  if (!recs.length) {
    $("detail").innerHTML = '<div class="empty">从左侧选择一个会话，查看它的多轮对话与每轮执行链路</div>';
    return;
  }
  const head =
    '<div style="padding:var(--s3);border-bottom:1px solid var(--hair)">' +
      '<div style="font-size:13px;font-weight:600">会话 ' + esc(SEL_SESS) + '</div>' +
      '<div style="font-size:11.5px;color:var(--text-3);margin-top:4px">共 ' + recs.length +
        ' 轮 · 最近 ' + esc(recs[0].created_at) + '</div>' +
    '</div>';
  const body = recs.slice().reverse().map(r =>
    '<details class="turn-card">' +
      '<summary>' +
        '<div class="turn-q">' + esc(r.message) + '</div>' +
        '<div class="turn-sub">' + esc(r.created_at) + '　·　' + (r.elapsed_ms / 1000).toFixed(1) + 's　·　' +
          ((r.tools_used || []).length ? esc((r.tools_used || []).join(" → ")) : "未调用工具") +
          '　·　点开看链路</div>' +
      '</summary>' +
      renderSteps(r.steps) +
    '</details>'
  ).join("");
  $("detail").innerHTML = head + body;
}

async function load() {
  try {
    const r = await fetch("/stats");
    const data = await r.json();
    STATS = data;
    const sess = data.sessions || [];
    if (!SEL_SESS && sess.length) SEL_SESS = sess[0].session_id;

    const mSig = JSON.stringify([data.total_requests, data.avg_elapsed_ms, data.tool_usage, sess.length]);
    if (mSig !== METRIC_SIG) { METRIC_SIG = mSig; renderMetrics(); }

    const lSig = sigList(data);
    if (lSig !== LIST_SIG) { LIST_SIG = lSig; renderList(); }

    const dSig = sigDetail(data);
    if (dSig !== DETAIL_SIG) { DETAIL_SIG = dSig; renderDetail(); }
  } catch (e) {
    $("sessList").innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>';
  }
}
load();
setInterval(load, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
