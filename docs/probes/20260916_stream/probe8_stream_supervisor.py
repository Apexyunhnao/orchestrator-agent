"""探针8：直接验 supervisor.stream_supervisor 的事件序列（只读调用，不改文件）。
关注：token 是否只来自汇总轮、决策轮的英文/工具参数是否被吞掉、事件时序。
"""
import asyncio
import os
import sys
import time

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))  # → orchestrator-agent 仓库根
sys.path.insert(0, PROJ)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.chdir(PROJ)

from supervisor import stream_supervisor  # noqa: E402

QUESTIONS = [
    "退货要什么条件？帮我把 ORD-1003 退了",
    "你好",
]


async def run_one(q: str, idx: int) -> None:
    print("\n" + "=" * 74)
    print(f"问题 #{idx}: {q}")
    print("=" * 74)
    t0 = time.time()
    text = []
    n_tok = 0
    kinds: dict = {}
    async for ev in _collect(q):
        dt = time.time() - t0
        k = ev["type"]
        kinds[k] = kinds.get(k, 0) + 1
        if k == "token":
            n_tok += 1
            text.append(ev["text"])
            continue
        if k == "status":
            print(f"  [{dt:5.2f}s] status  调用工具 → {ev['detail']}")
        elif k == "revoke":
            print(f"  [{dt:5.2f}s] REVOKE  ← 正文被撤回（兜底触发）")
        elif k == "done":
            print(f"  [{dt:5.2f}s] done    answer={len(ev['answer'])} 字 "
                  f"tools={ev['tools_used']} steps={[s['node'] for s in ev['steps']]} "
                  f"elapsed={ev['elapsed_ms']}ms")
        elif k == "error":
            print(f"  [{dt:5.2f}s] ERROR   {ev['message']}")
    print(f"  ---- token 事件 {n_tok} 个，正文拼接 {len(''.join(text))} 字 ----")
    print(f"  正文 = {''.join(text)[:200]!r}")
    assert "".join(text) == _last_done.get("answer", ""), "流式拼接与 done.answer 不一致！"


_last_done: dict = {}


async def _collect(q: str):
    async for ev in stream_supervisor(q):
        if ev["type"] == "done":
            _last_done.clear()
            _last_done.update(ev)
        yield ev


async def main() -> None:
    for i, q in enumerate(QUESTIONS, 1):
        await run_one(q, i)


asyncio.run(main())
