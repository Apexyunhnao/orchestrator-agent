"""
Supervisor Agent — LangGraph 多轮决策编排。
每轮决定调用 query_rag / create_ticket 或结束对话。
"""

import os
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph, add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from tools import query_rag, create_ticket

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
- 只有当用户的话题与上述两个工具的能力完全无关（例如闲聊、投诉客服态度）时，才直接用文字回复"""


# ── 节点函数 ──────────────────────────────────────────

def supervisor_node(state: SupervisorState) -> dict:
    """
    Supervisor 决策节点。
    将 system prompt + 历史消息发给 LLM，LLM 决定调用工具或直接回复。
    """
    llm = _get_llm()
    # 构建完整消息列表：system prompt + 对话历史
    full_messages = [{"role": "system", "content": SUPERVISOR_PROMPT}] + state["messages"]
    response = llm.invoke(full_messages)
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
    """构建并编译 Supervisor 状态图。"""
    graph = StateGraph(SupervisorState)

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


def run_supervisor_with_trace(user_message: str) -> tuple[str, list[str]]:
    """运行 Supervisor，返回 (最终回复, 工具调用列表)。"""
    from langchain_core.messages import HumanMessage

    initial_state = {"messages": [HumanMessage(content=user_message)]}
    result = _graph.invoke(initial_state)
    return _extract_final_answer(result), _extract_tools_used(result)


def run_supervisor(user_message: str) -> str:
    """
    运行 Supervisor，处理用户消息并返回最终回复。

    Args:
        user_message: 用户输入的自然语言消息

    Returns:
        Supervisor 最终汇总回复的文本内容
    """
    answer, _ = run_supervisor_with_trace(user_message)
    return answer
