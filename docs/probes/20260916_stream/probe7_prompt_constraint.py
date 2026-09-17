"""探针7：给 system prompt 追加「调工具时禁止输出文字」约束，看决策轮 content 能否变空。
只读，不改项目文件。同时报告 content / tool_call_chunk 的到达顺序（决定边流边判可行性）。
"""
import asyncio
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

EXTRA = "\n\n【输出格式硬约束】\n当你决定调用工具时，那一轮**不要输出任何解释性文字**（content 必须为空字符串），直接给出工具调用参数。\n只有在所有工具都调用完毕后，才用中文写最终回答给用户。"

QUESTIONS = [
    "退货要什么条件？帮我把 ORD-1003 退了",
    "ORD-1006 的物流怎么还没到",
    "帮我查一下 ORD-1005 是什么商品",
]
LLM_KW = dict(model="deepseek-chat", base_url="https://api.deepseek.com",
              api_key=os.getenv("DEEPSEEK_API_KEY"), temperature=0.0, timeout=30, streaming=True)
_msg = [{"role": "system", "content": S.SUPERVISOR_PROMPT + EXTRA}]


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


async def run_one(q: str, idx: int) -> None:
    print("\n" + "=" * 74)
    print(f"问题 #{idx}: {q}")
    print("=" * 74)
    runs: dict = {}
    order = 0
    seq: dict = {}
    async for ev in graph.astream_events({"messages": [HumanMessage(content=q)]}, version="v2"):
        k = ev["event"]
        if k == "on_chat_model_start":
            order += 1
            rid = str(ev["run_id"])
            runs[rid] = {"i": order, "content": [], "tcc": 0,
                         "step": (ev.get("metadata") or {}).get("langgraph_step")}
            seq[rid] = []
        elif k == "on_chat_model_stream":
            rid = str(ev["run_id"])
            if rid not in runs:
                order += 1
                runs[rid] = {"i": order, "content": [], "tcc": 0, "step": None}
                seq[rid] = []
            ch = ev["data"]["chunk"]
            c = getattr(ch, "content", "") or ""
            if c:
                runs[rid]["content"].append(c)
                if len(seq[rid]) < 6:
                    seq[rid].append("C")
            if getattr(ch, "tool_call_chunks", None):
                runs[rid]["tcc"] += 1
                if len(seq[rid]) < 6:
                    seq[rid].append("T")
    for rid, r in sorted(runs.items(), key=lambda kv: kv[1]["i"]):
        text = "".join(r["content"])
        role = "决策轮" if r["tcc"] else "汇总轮"
        print(f"  #{r['i']} [{role}] step={r['step']} content长度={len(text)} tcc={r['tcc']}")
        print(f"        前 6 个 chunk 类型序列: {''.join(seq[rid]) or '(空)'}")
        if text:
            print(f"        content = {text[:90]!r}")


async def main() -> None:
    for i, q in enumerate(QUESTIONS, 1):
        await run_one(q, i)


asyncio.run(main())
