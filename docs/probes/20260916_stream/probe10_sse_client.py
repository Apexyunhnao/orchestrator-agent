"""探针10：HTTP 层端到端测 /chat/stream（真流式验证 + 时序）"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8010"
Q = sys.argv[1] if len(sys.argv) > 1 else "退货要什么条件？帮我把 ORD-1003 退了"

t0 = time.time()
n_tok = 0
first_tok_at = None
text = []

with httpx.stream(
    "POST", f"{BASE}/chat/stream",
    json={"message": Q, "user_identifier": "", "session_id": "probe-e2e"},
    timeout=180.0,
) as r:
    print(f"HTTP {r.status_code}  content-type={r.headers.get('content-type')}")
    for line in r.iter_lines():
        if not line.startswith("data: "):
            continue
        d = json.loads(line[6:])
        dt = time.time() - t0
        t = d["type"]
        if t == "token":
            n_tok += 1
            if first_tok_at is None:
                first_tok_at = dt
                print(f"  [{dt:6.2f}s] 首 token = {d['text']!r}")
            text.append(d["text"])
        elif t == "status":
            print(f"  [{dt:6.2f}s] status  调用工具 → {d['detail']}")
        elif t == "revoke":
            print(f"  [{dt:6.2f}s] REVOKE（兜底触发）")
        elif t == "done":
            print(f"  [{dt:6.2f}s] done    answer={len(d['answer'])}字 tools={d['tools_used']} "
                  f"steps={[s['node'] for s in d['steps']]} elapsed={d['elapsed_ms']}ms")
        elif t == "error":
            print(f"  [{dt:6.2f}s] ERROR   {d['message']}")

total = time.time() - t0
print(f"\n首 token 延迟 = {first_tok_at:.2f}s" if first_tok_at else "\n没有任何 token！")
print(f"token 事件 {n_tok} 个，正文 {len(''.join(text))} 字，总耗时 {total:.2f}s")
print(f"正文预览 = {''.join(text)[:150]!r}")
if "I'll" in "".join(text) or "I will" in "".join(text):
    print("❌ 决策轮英文泄漏！")
else:
    print("✅ 决策轮内容未泄漏")
