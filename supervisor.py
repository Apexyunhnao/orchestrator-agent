"""
Supervisor Agent — LangGraph 多轮决策编排。
每轮决定调用 query_rag / create_ticket 或结束对话。
"""

import os
import time
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph, add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools import RequestContext, query_rag, create_ticket

load_dotenv()

# ── State ─────────────────────────────────────────────

class SupervisorState(TypedDict):
    """Supervisor 状态：消息历史，使用 add_messages reducer 自动拼接。"""
    messages: Annotated[list[BaseMessage], add_messages]


# ── LLM ───────────────────────────────────────────────

_llm: ChatOpenAI | None = None

def _get_llm() -> ChatOpenAI:
    """延迟初始化 LLM，bind 两个子Agent工具。"""
    global _llm
    if _llm is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY 环境变量")
        tools = [query_rag, create_ticket]
        _llm = ChatOpenAI(
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key=api_key,
            temperature=0.0,
            timeout=30,
            streaming=True,
        ).bind_tools(tools)
    return _llm


# ── System Prompt ─────────────────────────────────────

SUPERVISOR_PROMPT = """你是电商售后客服的总协调人。你可以调用两个工具：

- query_rag: 查客服政策（退货/退款/换货/物流规则）
- create_ticket: 办业务（查订单/改地址/退差价/催物流/取消订单/售后维修）

处理用户请求的决策流程：
1. 如果用户问政策类问题（退货条件、退款时限、换货规则、物流时效），先调用 query_rag 查政策
2. 如果用户要求具体操作（查订单状态、改收货地址、退差价、催物流），调用 create_ticket 建工单
3. 如果用户一句话里既有政策咨询又有业务操作（如"退货要什么条件？帮我把ORD-2001退了"），先查政策再办业务——不要漏掉任何一个
4. 所有必要的工具调用完毕后，基于工具返回的事实信息，给用户一个简洁、完整的中文汇总回答
5. 回答完毕不要再调工具，结束对话

注意：
- 不要猜测政策内容，必须通过 query_rag 查询
- 如果一个工具返回了错误或"暂不可用"，要在汇总里诚实告知用户该服务暂时不可用
- 回答要自然、友好，直接解决用户的问题

工具优先原则：
- 即使用户表述模糊、口语化、情绪化，或者没提供订单号，也应当先调用工具去查、去办，不要在还没查之前就反问用户或直接给结论——缺订单号时可以先建工单由业务侧补全
- 只有当用户的话题与上述两个工具的能力完全无关（例如闲聊、投诉客服态度）时，才直接用文字回复

输出格式硬约束（必须严格遵守）：
- 在你决定调用工具的那一轮，不要输出任何解释性文字（content 必须为空字符串），直接给出工具调用
- 只有在所有必要工具都调用完毕之后，才用中文写最终回答给用户
- 不要用英文输出任何内容"""


# ── 节点函数 ──────────────────────────────────────────

def supervisor_node(state: SupervisorState, config: RunnableConfig) -> dict:
    """
    Supervisor 决策节点。
    将 system prompt + 历史消息发给 LLM，LLM 决定调用工具或直接回复。

    注意：config 必须显式接收并透传给 llm.invoke —— LangGraph 的同步节点跑在线程池里，
    contextvar 里的事件回调不会自动传播，不透传 config 的话流式层收不到任何 token 事件。
    """
    llm = _get_llm()
    # 构建完整消息列表：system prompt + 对话历史
    full_messages = [{"role": "system", "content": SUPERVISOR_PROMPT}] + state["messages"]
    response = llm.invoke(full_messages, config=config)
    return {"messages": [response]}


# ── 路由判断 ──────────────────────────────────────────

def _route_after_supervisor(state: SupervisorState) -> str:
    """检查最后一条消息是否包含 tool_calls，决定下一步。"""
    last_msg = state["messages"][-1]
    if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
        print(f"[supervisor] LLM 决定调用工具: {[tc['name'] for tc in last_msg.tool_calls]}")
        return "tools"
    print("[supervisor] LLM 结束对话")
    return END


# ── 构建 Graph ────────────────────────────────────────

def _build_graph() -> StateGraph:
    """构建并编译 Supervisor 状态图。

    context_schema=RequestContext：把「本次请求的身份」（trace_id / token / owner_id）
    作为**单次调用的不可变上下文**注入图，随 runtime 直达工具内部。
    调用方通过 `graph.invoke(..., context=make_context(...))` 传入；模型看不到、也改不了。
    """
    graph = StateGraph(SupervisorState, context_schema=RequestContext)

    # 节点
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("tools", ToolNode([query_rag, create_ticket]))

    # 入口
    graph.set_entry_point("supervisor")

    # 条件边：supervisor 之后 → tools 或 END
    graph.add_conditional_edges("supervisor", _route_after_supervisor)
    # tools 之后 → 回到 supervisor 继续决策
    graph.add_edge("tools", "supervisor")

    return graph.compile()


_graph = _build_graph()


# ── 公共接口 ──────────────────────────────────────────

def _extract_final_answer(result: dict) -> str:
    """从 graph 结果中提取最后一条 AI 回复文本。"""
    for msg in reversed(result["messages"]):
        if hasattr(msg, "content") and msg.content and not hasattr(msg, "tool_calls"):
            return msg.content
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            continue
    return "抱歉，处理出错，请稍后再试。"


def _extract_tools_used(result: dict) -> list[str]:
    """从 graph 结果中提取所有被调用的工具名。"""
    tools: list[str] = []
    for msg in result["messages"]:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                if name and name not in tools:
                    tools.append(name)
    return tools


def run_supervisor_with_trace(user_message: str, ctx: RequestContext) -> tuple[str, list[str]]:
    """运行 Supervisor，返回 (最终回复, 工具调用列表)。

    ctx 必填：身份上下文由调用方提供，缺失即无法构造合法下游请求。
    """
    from langchain_core.messages import HumanMessage

    initial_state = {"messages": [HumanMessage(content=user_message)]}
    result = _graph.invoke(initial_state, context=ctx)
    return _extract_final_answer(result), _extract_tools_used(result)


def _step_item(m) -> dict:
    """把一条 message 转成 /ops 观测台用的步骤明细项。"""
    item: dict = {"type": type(m).__name__}
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        item["tool_calls"] = [
            {"name": tc.get("name", ""), "args": tc.get("args", {})}
            for tc in tcs
        ]
    name = getattr(m, "name", None)
    if name:
        item["name"] = name
    content = getattr(m, "content", "") or ""
    if content:
        item["content"] = content
    return item


def run_supervisor_with_steps(user_message: str, ctx: RequestContext) -> tuple[str, list[dict]]:
    """运行 Supervisor，返回 (最终回复, 执行步骤列表)。

    与 run_supervisor_with_trace 的区别：改用 LangGraph 的 stream 逐个消费节点事件，
    因此能拿到「一次请求内部经历了哪些步骤、每步调用了什么工具、每步耗时多久」。

    steps 每项结构：
        {"node": "supervisor"|"tools",
         "step_ms": 本步耗时, "total_ms": 累计耗时,
         "items": [{"type": "AIMessage"|"ToolMessage", "tool_calls": [...], "content": "...", "name": "..."}]}
    """
    from langchain_core.messages import HumanMessage

    initial_state = {"messages": [HumanMessage(content=user_message)]}
    all_messages: list = []
    steps: list[dict] = []
    t0 = time.time()
    prev = t0

    for chunk in _graph.stream(initial_state, stream_mode="updates", context=ctx):
        for node, update in chunk.items():
            now = time.time()
            msgs = update.get("messages", []) if isinstance(update, dict) else []
            items: list[dict] = []
            for m in msgs:
                items.append(_step_item(m))
            steps.append({
                "node": node,
                "step_ms": int((now - prev) * 1000),
                "total_ms": int((now - t0) * 1000),
                "items": items,
            })
            prev = now
            all_messages.extend(msgs)

    # 复用既有的答案/工具提取逻辑，保证行为与 run_supervisor_with_trace 一致
    return _extract_final_answer({"messages": all_messages}), steps


def run_supervisor(user_message: str, ctx: RequestContext) -> str:
    """
    运行 Supervisor，处理用户消息并返回最终回复。

    Args:
        user_message: 用户输入的自然语言消息
        ctx: 本次请求的身份上下文（trace_id / token / owner_id）

    Returns:
        Supervisor 最终汇总回复的文本内容
    """
    answer, _ = run_supervisor_with_trace(user_message, ctx)
    return answer


# ── 流式接口（SSE 用） ─────────────────────────────────

async def stream_supervisor(user_message: str, ctx: RequestContext):
    """异步生成器：把一次 Supervisor 执行拆成可逐步推送给前端的事件。

    事件类型：
        {"type": "status", "phase": "tool", "detail": "<工具名>"}   # 正在调用工具
        {"type": "token",  "text": "..."}                          # 给用户看的正文增量
        {"type": "revoke"}                                         # 撤回本轮已推送的正文（兜底）
        {"type": "done",   "answer": ..., "tools_used": [...], "steps": [...], "elapsed_ms": ...}
        {"type": "error",  "message": "..."}

    过滤规则（实测得出，见 docs 里的探针结论）：
      - 只看 metadata.langgraph_node == "supervisor" 的 chat model 事件
      - 同一轮里出现 tool_call_chunk ⇒ 这轮是「决策轮」，它的正文是给系统看的，绝不能给用户看
      - 实测决策轮 chunk 序列恒为全 T、汇总轮恒为全 C，所以用「首个 chunk」判定即可
      - revoke 是极端兜底：万一正文先出、之后才冒出 tool_call_chunk
    """
    from langchain_core.messages import HumanMessage

    initial_state = {"messages": [HumanMessage(content=user_message)]}
    steps: list[dict] = []
    all_messages: list = []
    text_parts: list[str] = []
    runs: dict[str, dict] = {}
    tools_used: list[str] = []
    node_t0: dict[str, float] = {}
    t0 = time.time()

    try:
        async for ev in _graph.astream_events(initial_state, version="v2", context=ctx):
            kind = ev["event"]
            node = (ev.get("metadata") or {}).get("langgraph_node")
            name = ev.get("name")

            # 节点耗时/步骤：必须按 chain 的 name 过滤。langgraph_node 会被节点内部的子 chain
            # （例如 ChatOpenAI 的 RunnableBinding）继承，只按它过滤会多记一笔。
            if kind == "on_chain_start" and name in ("supervisor", "tools"):
                node_t0[name] = time.time()

            elif kind == "on_chain_end" and name in ("supervisor", "tools"):
                update = (ev.get("data") or {}).get("output")
                msgs = update.get("messages", []) if isinstance(update, dict) else []
                now = time.time()
                steps.append({
                    "node": name,
                    "step_ms": int((now - node_t0.get(name, now)) * 1000),
                    "total_ms": int((now - t0) * 1000),
                    "items": [_step_item(m) for m in msgs],
                })
                all_messages.extend(msgs)
                for m in msgs:
                    for tc in (getattr(m, "tool_calls", None) or []):
                        n = tc.get("name", "")
                        if n and n not in tools_used:
                            tools_used.append(n)

            elif kind == "on_tool_start":
                yield {"type": "status", "phase": "tool", "detail": ev.get("name") or ""}

            elif kind == "on_chat_model_stream":
                if node != "supervisor":
                    continue
                rid = str(ev.get("run_id"))
                st = runs.setdefault(rid, {"kind": None, "emitted": False, "start": 0})
                chunk = ev["data"]["chunk"]
                tcc = getattr(chunk, "tool_call_chunks", None)
                content = getattr(chunk, "content", "") or ""

                if tcc:
                    st["kind"] = "tool"
                    if st["emitted"]:
                        del text_parts[st["start"]:]   # 撤回本轮已推送的正文
                        st["emitted"] = False
                        yield {"type": "revoke"}
                elif content:
                    if st["kind"] is None:
                        st["kind"] = "text"
                        st["start"] = len(text_parts)
                    if st["kind"] == "text":
                        st["emitted"] = True
                        text_parts.append(content)
                        yield {"type": "token", "text": content}

    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        return

    answer = "".join(text_parts) or _extract_final_answer({"messages": all_messages})
    yield {
        "type": "done",
        "answer": answer,
        "tools_used": tools_used,
        "steps": steps,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
