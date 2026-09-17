"""探针2：为什么 astream_events 抓不到 chat model 事件？
只读，不改项目文件。三组对照：
  A. 版本号
  B. 直接对 _get_llm() 做 astream_events（v1 / v2 都试）
  C. 直接对 _get_llm() 做 astream（token 级）
"""
import asyncio
import collections
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))  # → orchestrator-agent 仓库根
sys.path.insert(0, PROJ)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.chdir(PROJ)

import importlib.metadata as md  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402
import supervisor as S  # noqa: E402

for pkg in ("langchain-core", "langchain-openai", "langgraph", "langchain"):
    try:
        print(f"  {pkg:<20} {md.version(pkg)}")
    except Exception as e:  # noqa: BLE001
        print(f"  {pkg:<20} <{e}>")

MSGS = [{"role": "system", "content": "你是一个助手，只回答：收到"}, HumanMessage(content="你好")]


async def probe_events(tag: str, runnable, version: str) -> None:
    c = collections.Counter()
    try:
        async for ev in runnable.astream_events(MSGS if tag.startswith("B") else {"messages": MSGS}, version=version):
            c[ev["event"]] += 1
    except Exception as e:  # noqa: BLE001
        print(f"  [{tag}] 异常: {type(e).__name__}: {e}")
    print(f"  [{tag}] events: {dict(c)}")


async def probe_astream_tokens(tag: str) -> None:
    llm = S._get_llm()
    n = 0
    texts = []
    try:
        async for chunk in llm.astream(MSGS):
            n += 1
            if chunk.content:
                texts.append(chunk.content)
    except Exception as e:  # noqa: BLE001
        print(f"  [{tag}] 异常: {type(e).__name__}: {e}")
    print(f"  [{tag}] chunk 数={n} 内容={''.join(texts)[:60]!r}")


async def main() -> None:
    llm = S._get_llm()
    print("B1 bind_tools 后的 llm.astream_events v2:")
    await probe_events("B1-v2", llm, "v2")
    print("B2 bind_tools 后的 llm.astream_events v1:")
    await probe_events("B2-v1", llm, "v1")
    print("C  llm.astream（原生 token 流）:")
    await probe_astream_tokens("C")


asyncio.run(main())
