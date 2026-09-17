"""最小认证与权限 —— JWT + HttpOnly Cookie。

设计取舍：

1. **无状态 JWT**：服务端不存 session，token 自包含 username / role / 过期时间，验签即可信。
2. **HttpOnly + SameSite=Strict Cookie**，而不是 localStorage：前端 JS 读不到 token，
   降低 XSS 窃取面；SameSite=Strict 挡住跨站携带，降低 CSRF 面。
3. **密码只存 bcrypt 哈希**：代码里不出现明文口令（演示口令写在 README，属公开演示凭据）。
4. **下游服务各自验签**（共享 AUTH_SECRET）：p2 / rag 不信任编排层传来的角色字符串，
   拿到 token 自己验 —— 这是纵深防御：上游被绕过时下游仍然安全。

已知限制（演示环境，如实记录）：
- 只做短期 access token，未做可撤销的 refresh token 与会话表
- 未做登录限流（任务书第五章要求，属后续项）
- AUTH_SECRET 未配置时**拒绝启动**（fail-closed）—— 不再回落到固定默认值
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, HTTPException, status

# 本模块自己加载 .env，保证无论被谁、以什么顺序导入都能读到正确的 AUTH_SECRET。
# 踩过的坑：main.py 的 `from auth import ...` 排在 `from supervisor import ...`（supervisor 里才
# load_dotenv）之前 → auth 读不到环境变量、回落到默认密钥，于是它签发的 token 在共享同一
# secret 的下游服务（p2/rag）验签失败，表现为"p1 调用下游全部 401"。
load_dotenv()


def _require_auth_secret() -> str:
    """读取 AUTH_SECRET；未配置直接拒绝启动（fail-closed）。

    历史：这里原本回落到固定默认值 `dev-only-insecure-secret-change-me` ——
    任何人 clone 后不配密钥也能跑起来，而且"能跑"的样子和配好密钥完全一样，
    只是所有部署共用一个人人可见的密钥：知道默认值的人就能自己签一枚管理员 token。
    现在改成缺失即拒绝：**启动失败**比"看起来正常的假安全"好。
    """
    secret = (os.getenv("AUTH_SECRET") or "").strip()
    if not secret:
        raise RuntimeError(
            "\n[AUTH_SECRET 未配置] 服务拒绝启动（fail-closed）。\n"
            "  本地演示：运行 start-demo.bat —— 它会生成一次随机密钥并写入三仓 .env\n"
            "  手工配置：cp .env.example .env 后填入 AUTH_SECRET\n"
            "  生成密钥：python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "  ⚠️ 三个服务必须使用同一个 AUTH_SECRET（下游各自验签同一枚 JWT）\n"
        )
    return secret


SECRET_KEY: str = _require_auth_secret()
ALGORITHM = "HS256"
TOKEN_TTL_MINUTES = int(os.getenv("AUTH_TOKEN_TTL_MINUTES", "120"))
COOKIE_NAME = "svc_token"

# 三个演示账号：密码只存 bcrypt 哈希（明文口令见 README，公开演示凭据）
DEMO_USERS: dict[str, dict[str, str]] = {
    "customer": {
        "password_hash": "$2b$12$jxNvw8lEwm8NS5qfgwXyaesZ/I4pSPk4EEr0WmGZ7gAAhSyK6n4Yq",
        "role": "customer",
        "owner_id": "CUST-1001",
        "display": "客户 张敏",
    },
    "agent": {
        "password_hash": "$2b$12$czqVTch4ZJpkyHZ7blTJou9IRmRDNKeK9g7xXUGqeNGL6rU/5zFPy",
        "role": "agent",
        "owner_id": "AGENT-01",
        "display": "客服 李强",
    },
    "admin": {
        "password_hash": "$2b$12$cu0Lx0n0PKiA7bmHiinbr.PVRBGpS//savusiAw.KQTS.xbt2wdIG",
        "role": "admin",
        "owner_id": "ADMIN-01",
        "display": "管理员 王磊",
    },
}

ROLE_LABELS = {"customer": "客户", "agent": "客服", "admin": "管理员"}


# ── 口令校验 ───────────────────────────────────────────

def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """校验账号密码。成功返回用户信息（不含哈希），失败返回 None。

    注意：账号不存在时也走一次 bcrypt 校验，避免用响应时间区分"账号是否存在"。
    """
    rec = DEMO_USERS.get(username)
    if rec is None:
        bcrypt.checkpw(password.encode(), DEMO_USERS["customer"]["password_hash"].encode())
        return None
    if not bcrypt.checkpw(password.encode(), rec["password_hash"].encode()):
        return None
    return {
        "username": username,
        "role": rec["role"],
        "owner_id": rec["owner_id"],
        "display": rec["display"],
    }


# ── Token ─────────────────────────────────────────────

def create_access_token(user: dict[str, Any]) -> str:
    """签发 access token（HS256，默认 2 小时）。"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "owner_id": user["owner_id"],
        "display": user["display"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=TOKEN_TTL_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """验签 + 校验过期。失败抛 401（不区分"签名错/已过期"细节，避免给攻击者信息）。"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭证无效，请重新登录")


# ── FastAPI 依赖 ──────────────────────────────────────

def current_user(svc_token: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    """依赖：从 HttpOnly Cookie 取 token 并验签，返回当前用户（含原始 token，便于向下游透传）。"""
    if not svc_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    payload = decode_token(svc_token)
    return {
        "username": payload.get("sub", ""),
        "role": payload.get("role", ""),
        "owner_id": payload.get("owner_id", ""),
        "display": payload.get("display", ""),
        "token": svc_token,
    }


def require_roles(*roles: str):
    """依赖工厂：要求当前用户角色在 roles 内，否则 403。"""
    allowed = set(roles)

    def _guard(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user.get("role") not in allowed:
            cur = ROLE_LABELS.get(user.get("role", ""), user.get("role", ""))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"当前角色（{cur}）无权访问该资源",
            )
        return user

    return _guard
