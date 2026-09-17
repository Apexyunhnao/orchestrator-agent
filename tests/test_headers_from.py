"""_headers_from 契约测试：runtime context → 下游请求头。

被测量（tools.py::_headers_from）实际实现：

    ctx = dict(getattr(runtime, "context", None) or {})
    headers = {}
    if ctx.get("trace_id"):  headers["X-Trace-Id"]    = str(ctx["trace_id"])
    if ctx.get("token"):     headers["Authorization"] = "Bearer " + token
    if ctx.get("owner_id"):  headers["X-Owner-Id"]    = str(ctx["owner_id"])
    return headers

本文件只断言上面这段真实行为，不臆测：
  - 三个字段各自**独立**判真值 —— 缺一个不影响另外两个；
  - 判真值用 `if ctx.get(...)`，所以空字符串 / None / 0 都**不产出**该头；
  - trace_id / owner_id 走 `str()`，非字符串会被转成字符串；token 走 f-string 拼接；
  - 取不到 context（属性缺失、None、空 dict）时返回**空 dict**，下游自行处理 401。

⚠️ `_headers_from` 只读 context，不读任何模块级状态、不发请求，因此这里无需 mock，
直接喂假的 runtime 对象即可。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import _headers_from, make_context  # noqa: E402


class FakeRuntime:
    """只有 context 属性的最小 runtime 替身（LangGraph ToolRuntime 的鸭子类型）。"""

    def __init__(self, context):
        self.context = context


class NoContextRuntime:
    """完全没有 context 属性的 runtime —— 覆盖 getattr 的 default 分支。"""


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

    # ── 1. 空上下文 → 空 dict ──
    print("=" * 74)
    print("1) ctx = {} → 返回空字典")
    print("=" * 74)
    h = _headers_from(FakeRuntime({}))
    check("返回值是 dict", isinstance(h, dict), f"实际类型={type(h).__name__}")
    check("内容为空 {}", h == {}, f"实际={h!r}")
    check("一个头都没有", len(h) == 0, f"实际键={list(h)}")

    # ── 2. 仅 token → 恰好只有 Authorization ──
    print()
    print("=" * 74)
    print("2) 仅 token → 只有 Authorization 一个键")
    print("=" * 74)
    h = _headers_from(FakeRuntime({"token": "tk-123"}))
    check("键集合恰好是 {Authorization}", set(h) == {"Authorization"}, f"实际键={sorted(h)}")
    check("键数量 == 1", len(h) == 1, f"实际={len(h)}")
    check("值为 'Bearer tk-123'", h.get("Authorization") == "Bearer tk-123",
          f"实际={h.get('Authorization')!r}")
    check("不含 X-Trace-Id", "X-Trace-Id" not in h, f"实际={h!r}")
    check("不含 X-Owner-Id", "X-Owner-Id" not in h, f"实际={h!r}")

    # ── 3. 三字段齐全 ──
    print()
    print("=" * 74)
    print("3) token + trace_id + owner_id 齐全 → 三个头都正确")
    print("=" * 74)
    h = _headers_from(FakeRuntime({"trace_id": "trace-abc", "token": "tk-123",
                                   "owner_id": "owner-9"}))
    check("键集合 = {X-Trace-Id, Authorization, X-Owner-Id}",
          set(h) == {"X-Trace-Id", "Authorization", "X-Owner-Id"}, f"实际键={sorted(h)}")
    check("X-Trace-Id = trace-abc", h.get("X-Trace-Id") == "trace-abc",
          f"实际={h.get('X-Trace-Id')!r}")
    check("Authorization = 'Bearer tk-123'", h.get("Authorization") == "Bearer tk-123",
          f"实际={h.get('Authorization')!r}")
    check("X-Owner-Id = owner-9", h.get("X-Owner-Id") == "owner-9",
          f"实际={h.get('X-Owner-Id')!r}")

    # 用真实构造器交叉验证一次：make_context 产出的 TypedDict 同样可用
    h2 = _headers_from(FakeRuntime(make_context("trace-abc", "tk-123", "owner-9")))
    check("make_context 产出的 context 得到相同结果", h2 == h, f"实际={h2!r} vs {h!r}")

    # ── 4. 边界：runtime 缺 context / context 为 None ──
    print()
    print("=" * 74)
    print("4) 边界：runtime 没有 context 属性 / context 为 None / runtime 为 None")
    print("=" * 74)
    check("runtime 无 context 属性 → {}",
          _headers_from(NoContextRuntime()) == {},
          f"实际={_headers_from(NoContextRuntime())!r}")
    check("context = None → {}", _headers_from(FakeRuntime(None)) == {},
          f"实际={_headers_from(FakeRuntime(None))!r}")
    check("runtime 本身为 None → {}", _headers_from(None) == {},
          f"实际={_headers_from(None)!r}")

    # ── 5. 边界：空字符串 / 假值字段不产出对应头 ──
    print()
    print("=" * 74)
    print("5) 边界：空字符串 / 假值字段各自不产出头（三字段独立判真值）")
    print("=" * 74)
    h = _headers_from(FakeRuntime({"trace_id": "", "token": "", "owner_id": ""}))
    check("三个字段都是空字符串 → {}", h == {}, f"实际={h!r}")

    h = _headers_from(FakeRuntime({"trace_id": "", "token": "tk-123", "owner_id": ""}))
    check("只有 token 非空 → 键集合 = {Authorization}",
          set(h) == {"Authorization"}, f"实际键={sorted(h)}")

    h = _headers_from(FakeRuntime({"trace_id": "trace-x", "token": "", "owner_id": "owner-x"}))
    check("token 为空串时仍产出 trace/owner 头，且无 Authorization",
          set(h) == {"X-Trace-Id", "X-Owner-Id"} and "Authorization" not in h,
          f"实际={h!r}")

    h = _headers_from(FakeRuntime({"trace_id": None, "token": None, "owner_id": None}))
    check("三个字段都是 None → {}", h == {}, f"实际={h!r}")

    h = _headers_from(FakeRuntime({"trace_id": 0, "token": 0, "owner_id": 0}))
    check("假值 0 不产出头 → {}", h == {}, f"实际={h!r}")

    # ── 6. 边界：非字符串值走 str() / 拼接 ──
    print()
    print("=" * 74)
    print("6) 边界：非字符串值被 str() 化")
    print("=" * 74)
    h = _headers_from(FakeRuntime({"trace_id": 42, "token": 7, "owner_id": 3.5}))
    check("trace_id=42 → '42'", h.get("X-Trace-Id") == "42", f"实际={h.get('X-Trace-Id')!r}")
    check("token=7 → 'Bearer 7'", h.get("Authorization") == "Bearer 7",
          f"实际={h.get('Authorization')!r}")
    check("owner_id=3.5 → '3.5'", h.get("X-Owner-Id") == "3.5", f"实际={h.get('X-Owner-Id')!r}")
    check("所有值都是 str", all(isinstance(v, str) for v in h.values()),
          f"实际={ {k: type(v).__name__ for k, v in h.items()} }")

    # ── 7. 边界：context 是 TypedDict / dict 子类也能被 dict() 接收 ──
    print()
    print("=" * 74)
    print("7) 边界：context 为 dict 子类（TypedDict 的真身就是 dict）")
    print("=" * 74)

    class MyCtx(dict):
        pass

    h = _headers_from(FakeRuntime(MyCtx({"trace_id": "t", "token": "k", "owner_id": "o"})))
    check("dict 子类 context 正常产头",
          h == {"X-Trace-Id": "t", "Authorization": "Bearer k", "X-Owner-Id": "o"},
          f"实际={h!r}")

    # 返回值是**新** dict，改它不污染入参
    src = {"trace_id": "t", "token": "k", "owner_id": "o"}
    out = _headers_from(FakeRuntime(src))
    out["X-Extra"] = "x"
    check("返回的是新 dict，改动不回写调用方的 context",
          "X-Extra" not in src, f"实际={src!r}")

    print()
    print("=" * 74)
    print(f"结果：{passed}/{passed + failed} 通过")
    print("=" * 74)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main_test())
