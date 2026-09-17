"""探针6：两件事
  Q1. on_chat_model_start 的 metadata 里有没有 langgraph_node（决定过滤手段是否trivial）
  Q2. 决策轮"先吐一句解释文字"是稳定现象还是偶发（3 个不同问题各跑一次）
只读，不改项目文件。
"""
import asyncio
import collections
import json
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

QUESTIONS = [
    "退货要什么条件？帮我把 ORD-1003 退了",
    "ORD-1006 的物流怎么还没到",
    "你好",
]
LLM_KW = dict(model="deepseek-chat", base_url="https://api.deepseek.com",
              api_key=os.getenv("DEEPSEEK_API_KEY"), temperature=0.0, timeout=30, streaming=True)
_msg = [{"role": "system", "content": S.SUPERVISOR_PROMPT}]


def node(state, config):
    llm = ChatOpenAI(**LLM_KW).bind_tools([query_rag, create_ticket])
    return {"messages": [llm.invoke(_msg + state["messages"], config=config)]}


g = StateGraph(S.SupervisorState)
g.add_node("supervisor", node)
g.add_node("tools", ToolNode([query_rag, create_ticket]))
g.set_entry_point("supervisor")
g.add_conditional_edges("supervisor", S._route_after_supervisor)
g.add_edge("tools", "supervisor")
graph = g.compile()


def short(d: dict) -> dict:
    keep = ("langgraph_node", "langgraph_step", "ls_provider", "ls_model_name")
    return {k: d.get(k) for k in keep if k in d}


async def run_one(q: str, idx: int) -> None:
    print("\n" + "=" * 74)
    print(f"问题 #{idx}: {q}")
    print("=" * 74)
    runs: dict = {}
    order = 0
    meta_shown = False
    async for ev in graph.astream_events({"messages": [HumanMessage(content=q)]}, version="v2"):
        k = ev["event"]
        if k == "on_chat_model_start":
            order += 1
            # noqa: E501
            runs[str(ev["run_id"])] = {"i": order, "content": [], "tcc": 0,
                                       "meta": short(ev.get("metadata") or {}),
                                       "tags": tuple(ev.get("tags") or [])}
        elif k == "on_chat_model_stream":
            rid = str(ev["run_id"])
            if rid not in runs:
                order += 1
                runs[rid] = {"i": order, "content": [], "tcc": 0, "meta": {}, "tags": ()}
            ch = ev["data"]["chunk"]
            c = getattr(ch, "content", "") or ""
            if c:
                runs[rid]["content"].append(c)
            if getattr(ch, "tool_call_chunks", None):
                runs[rid]["tcc"] += 1
    for rid, r in sorted(runs.items(), key=lambda kv: kv[1]["i"]):
        text = "".join(r["content"])
        role = "决策轮" if r["tcc"] else "汇总轮"
        print(f"  #{r['i']} [{role}] metadata={json.dumps(r['meta'], ensure_ascii=False)} "
              f"tags={r['tags']}")
        print(f"        content 长度={len(text)} tcc={r['tcc']} | {text[:110]!r}")


async def main() -> None:
    for i, q in enumerate(QUESTIONS, 1):
        await run_one(q, i)


asyncio.run(main())
