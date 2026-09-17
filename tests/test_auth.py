"""P1 最小认证与权限 —— 自动化测试。

用 FastAPI TestClient 直接打应用：不需要起服务、不触发真实 LLM 调用（依赖校验在业务逻辑之前）。

覆盖任务书第五章「必须验证的权限边界」：
- 未登录访问业务接口 → 401
- 账号/密码错误 → 401，且不区分“账号不存在”
- customer 不能访问运营数据与观测台 → 403
- agent / admin 可以访问
- token 被篡改 / 过期 → 401
- 登录 Cookie 具备 HttpOnly + SameSite=Strict

运行：python tests/test_auth.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from auth import ALGORITHM, COOKIE_NAME, SECRET_KEY, TOKEN_TTL_MINUTES  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    RESULTS.append((name, ok, extra))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"   [{extra}]" if extra else ""))


def new_client() -> TestClient:
    return TestClient(main.app)


def login(username: str, password: str):
    c = new_client()
    r = c.post("/auth/login", json={"username": username, "password": password})
    return c, r


def main_test() -> int:
    main.init_db()   # 保证表存在（TestClient 不走 lifespan）

    print("=" * 74)
    print("1) 未登录 —— 业务接口与运营接口都必须 401")
    print("=" * 74)
    c = new_client()
    check("未登录 POST /gateway → 401", c.post("/gateway", json={"message": "你好啊朋友"}).status_code == 401,
          f"got {c.post('/gateway', json={'message': '你好啊朋友'}).status_code}")
    check("未登录 POST /chat/stream → 401", c.post("/chat/stream", json={"message": "你好啊朋友"}).status_code == 401)
    check("未登录 GET /stats → 401", c.get("/stats").status_code == 401)
    check("未登录 GET /ops → 401", c.get("/ops").status_code == 401)
    check("未登录 GET /auth/me → 401", c.get("/auth/me").status_code == 401)

    print()
    print("=" * 74)
    print("2) 登录失败路径")
    print("=" * 74)
    _, r = login("customer", "wrong-password")
    check("错误密码 → 401", r.status_code == 401)
    _, r = login("nobody", "customer123")
    check("不存在的账号 → 401（且提示与密码错相同）", r.status_code == 401,
          str(r.json().get("detail", ""))[:20])
    check("两种失败的提示一致（不泄露账号是否存在）",
          login("customer", "wrong-password")[1].json().get("detail")
          == login("nobody", "customer123")[1].json().get("detail"))

    print()
    print("=" * 74)
    print("3) 登录成功 + Cookie 安全属性")
    print("=" * 74)
    c, r = login("customer", "customer123")
    check("customer 登录成功 → 200", r.status_code == 200, r.text[:60])
    sc = r.headers.get("set-cookie", "")
    check("Cookie 带 HttpOnly", "httponly" in sc.lower())
    check("Cookie 带 SameSite=Strict", "samesite=strict" in sc.lower())
    check("Cookie 名正确", COOKIE_NAME in sc)
    check("响应体不含 token（token 只在 HttpOnly Cookie 里）", "token" not in r.text.lower())

    me = c.get("/auth/me")
    check("登录后 GET /auth/me → 200", me.status_code == 200, str(me.json())[:70])
    check("/auth/me 返回角色为 customer", me.json().get("role") == "customer")

    print()
    print("=" * 74)
    print("4) 权限边界（任务书第五章逐条）")
    print("=" * 74)
    c_cust, _ = login("customer", "customer123")
    check("customer 访问 /stats（运营数据）→ 403", c_cust.get("/stats").status_code == 403,
          f"got {c_cust.get('/stats').status_code}")
    check("customer 访问 /ops（观测台）→ 403", c_cust.get("/ops").status_code == 403)

    c_agent, _ = login("agent", "agent123")
    check("agent 访问 /stats → 200", c_agent.get("/stats").status_code == 200)
    check("agent 访问 /ops → 200", c_agent.get("/ops").status_code == 200)

    c_admin, _ = login("admin", "admin123")
    check("admin 访问 /stats → 200", c_admin.get("/stats").status_code == 200)
    check("admin 访问 /ops → 200", c_admin.get("/ops").status_code == 200)

    print()
    print("=" * 74)
    print("5) Token 篡改与过期")
    print("=" * 74)
    c = new_client()
    c.cookies.set(COOKIE_NAME, "eyJhbGciOiJIUzI1NiJ9.tampered.signature")
    check("篡改的 token → 401", c.get("/auth/me").status_code == 401)

    c = new_client()
    expired = jwt.encode(
        {"sub": "customer", "role": "customer", "owner_id": "", "display": "",
         "exp": int(time.time()) - 10},
        SECRET_KEY, algorithm=ALGORITHM,
    )
    c.cookies.set(COOKIE_NAME, expired)
    check("过期 token → 401", c.get("/auth/me").status_code == 401)

    c = new_client()
    wrong_key = jwt.encode(
        {"sub": "customer", "role": "customer", "owner_id": "", "display": "",
         "exp": int(time.time()) + 600},
        "another-secret", algorithm=ALGORITHM,
    )
    c.cookies.set(COOKIE_NAME, wrong_key)
    check("用别的密钥签的 token → 401（伪造被拒）", c.get("/auth/me").status_code == 401)

    print()
    print("=" * 74)
    print("6) 公开端点不受影响")
    print("=" * 74)
    c = new_client()
    check("未登录 GET /health → 200（健康检查公开）", c.get("/health").status_code == 200)
    check("未登录 GET / → 200（登录页公开）", c.get("/").status_code == 200)

    # ── 汇总 ──
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print()
    print("=" * 74)
    print(f"结果：{passed}/{total} 通过")
    print("=" * 74)
    for name, ok, extra in RESULTS:
        if not ok:
            print(f"  ❌ {name} {extra}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main_test())
