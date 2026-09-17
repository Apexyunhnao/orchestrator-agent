"""探针4：图里怎么才能收到 chat model 事件？三个变体对照（只读，不改项目文件）
  V0 现状复现：同步节点 + llm.invoke(msgs)        —— 预期：0 个 chat 事件
  V1 async 节点 + await llm.ainvoke(msgs)        —— 猜：contextvar 同线程 → 有事件
  V2 同步节点(state, config) + invoke(msgs, config) —— 猜：显式传 callbacks → 有事件
每个变体都用 streaming=True 的 LLM，便于同时看 token chunk。
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

from langchain_core.messages import HumanMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from langgraph.graph import END, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from tools import query_rag, create_ticket  # noqa: E402
import supervisor as S  # noqa: E402

USER_MSG = "退货要什么条件？帮我把 ORD-1003 退了"

LLM_KW = dict(
    model="deepseek-chat",
    base_url="https://api.deepseek.com",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    temperature=0.0,
    timeout=30,
    streaming=True,
)

_msg = [{"role": "system", "content": S.SUPERVISOR_PROMPT}]


def _node_plain(state):
    llm = ChatOpenAI(**LLM_KW).bind_tools([query_rag, create_ticket])
    return {"messages": [llm.invoke(_msg + state["messages"])]}


async def _node_async(state):
    llm = ChatOpenAI(**LLM_KW).bind_tools([query_rag, create_ticket])
    return {"messages": [await llm.ainvoke(_msg + state["messages"])]}


def _node_config(state, config):
    llm = ChatOpenAI(**LLM_KW).bind_tools([query_rag, create_ticket])
    return {"messages": [llm.invoke(_msg + state["messages"], config=config)]}


def build(node):
    g = StateGraph(S.SupervisorState)
    g.add_node("supervisor", node)
    g.add_node("tools", ToolNode([query_rag, create_ticket]))
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", S._route_after_supervisor)
    g.add_edge("tools", "supervisor")
    return g.compile()


async def run(label: str, node) -> None:
    kinds = collections.Counter()
    token_runs = collections.defaultdict(int)
    graph = build(node)
    try:
        async for ev in graph.astream_events({"messages": [HumanMessage(content=USER_MSG)]}, version="v2"):
            kinds[ev["event"]] += 1
            if ev["event"] == "on_chat_model_stream":
                token_runs[str(ev.get("run_id"))[:8]] += 1
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] 异常 {type(e).__name__}: {e}")
    chat_ev = {k: v for k, v in kinds.items() if "chat_model" in k}
    print(f"[{label}] chat_model 事件: {chat_ev or '无'}")
    print(f"[{label}] token chunk 总数={sum(token_runs.values())} 分属 {len(token_runs)} 个 run")
    print(f"[{label}] 其他事件: { {k: v for k, v in kinds.items() if 'chat_model' not in k} }")


async def main() -> None:
    await run("V0 同步invoke（现状）", _node_plain)
    await run("V1 async节点 ainvoke", _node_async)
    await run("V2 同步+config显式传", _node_config)


asyncio.run(main())
