# 智能客服统一入口 — 多Agent编排（Supervisor模式）

## 项目概述

一个 Supervisor Agent 协调两个子 Agent，处理客服场景的全链路需求。

## 架构

```
用户 → Supervisor Agent (LangGraph)
         │
         ├─ 可用工具:
         │   ├─ query_rag(question)    → RAG Agent :8002  查政策
         │   └─ create_ticket(message)  → 客服 Agent :8001  办业务
         │
         ├─ 第1轮: 分析用户请求 → 决定先调哪个工具
         ├─ 第2轮: 拿到结果 → 判断是否要调另一个工具
         └─ 最终轮: 所有结果齐了 → 合成回答 → finish
```

## 子Agent接口

### RAG Agent（:8002）— 客服政策知识库
文档: 退货政策、退款政策、换货政策、物流配送政策
POST /query {"question": "退货需要几天"}
→ {"answer": "...", "sources": ["退货政策.md"]}

### 客服工单 Agent（:8001）— 工单处理
POST /api/ticket {"message": "帮我把ORD-2001退了", "user_identifier": ""}
→ {"ticket_id": "...", "category": "售后", "status": "...", "resolution": "..."}

## 技术栈

- LangGraph (supervisor pattern)
- langgraph-supervisor 库或手写 supervisor state graph
- openai SDK (DeepSeek chat)
- httpx (调子Agent)
- FastAPI (对外接口)
- Python 3.10

## 文件结构

```
orchestrator-agent/
├── supervisor.py      # Supervisor Agent + LangGraph状态图
├── tools.py           # query_rag + create_ticket (包装HTTP调用)
├── main.py            # FastAPI入口 + Web页面
├── eval/
│   └── test_cases.json
├── docs/
│   ├── requirements.md
│   └── development-log.md
└── CLAUDE.md
```

## Supervisor 工作原理

1. State: {messages: [...], rag_result: null, ticket_result: null}
2. Supervisor LLM 每轮决策：看 messages → 选工具或 finish
3. 工具执行后结果写回 state → 下一轮 supervisor 基于新结果再决策
4. 最终 supervisor 调用 __end__ → 循环停止 → 返回合成回答

## 与旧方案的区别

| | 旧（路由） | 新（Supervisor） |
|---|-----------|-----------------|
| LLM调用 | 1次分类 | 3-5轮决策+合成 |
| 决策方式 | 三选一 | 多轮逐步 |
| 上下文 | 无 | RAG结果传给客服Agent |
| 深度 | if/else | 状态图+循环 |

## 依赖

```
langgraph>=0.2.0
langchain>=0.3.0
fastapi>=0.100.0
uvicorn>=0.20.0
openai>=1.0.0
httpx>=0.24.0
python-dotenv>=1.0.0
```

## 已有文件可删

classifier.py 和 router.py 不再需要。
