# 多 Agent 编排 Supervisor — 智能客服统一入口

## 功能

LangGraph Supervisor 模式协调两个子 Agent（RAG 查政策 + 客服建工单），多轮逐步决策。用户一句话"退货要几天，帮我把 ORD-2001 退了"→ Supervisor 先调 RAG 查退货政策 → 拿结果后调客服建工单 → 合成回答。

## 为什么做

两版迭代：v1 做成 HTTP 路由器（classify→forward→return），发现编排深度为零 → 推翻重做 v2 Supervisor。这不是"一开始就设计对"，而是"发现错了立刻改"。

## 架构

```
用户 → Supervisor (LangGraph)
         ├─ query_rag(question)    → RAG Agent :8002  查政策
         └─ create_ticket(message)  → 客服 Agent :8001  办业务
         
Supervisor 循环: 决策 → 调工具 → 拿结果 → 再决策 → ... → 合成
```

## 技术栈

- LangGraph（状态图 + 条件循环边）
- LangChain（Tool 封装）
- DeepSeek（Supervisor 决策）
- httpx（HTTP 调子 Agent，trust_env=False 绕代理）
- SQLite（请求日志，/stats 统计）
- FastAPI（对外接口）

## 启动

```bash
pip install -r requirements.txt
# 确保 RAG (:8002) 和客服 (:8001) 已启动
uvicorn main:app --port 8000
```

或 Docker 一键：
```bash
cd orchestrator-agent
docker-compose up -d    # 同时启动三个服务
```

## API

POST /gateway
```json
{"message": "退货要几天，帮我把ORD-2001退了"}
→ {"answer": "...", "tools_used": ["query_rag", "create_ticket"]}
```

GET /stats
```json
{"total_requests": 42, "recent_10": [...], "tool_usage": {"query_rag": 30, "create_ticket": 22}}
```

## 评估

| 指标 | 数值 |
|------|------|
| 工具调用准确率 | 30条测试 96.7% |
| 唯一失败 | S18 "退货运费谁出，有个单子要退" — 无订单号，LLM 未调 create_ticket |
| 全链路 e2e | 纯政策/纯业务/混合三场景全通 |

## 与 v1（路由）对比

| | v1 路由 | v2 Supervisor |
|---|--------|-------------|
| 架构 | 一次分类→转发 | 多轮决策→工具调用→再决策 |
| LLM 调用 | 1 次 | 3-5 次 |
| 上下文传递 | 无 | 子Agent结果送回继续决策 |
| 多步协作 | 不支持 | 先RAG查政策→再客服建工单 |

## 踩坑记录

1. deepseek-v4-flash 不存在 → 全空；deepseek-v4-pro 是推理模型 → token 烧在 reasoning_content → 全空 → deepseek-chat 才对
2. max_tokens=10 截断为空 → 分类兜底全判 knowledge → 200 才稳
3. httpx 走 Windows 系统代理 → localhost 返回 503 → trust_env=False 解决
4. 退差价误判：疑问句式被当政策查询 → prompt 加 "优先判 service" + few-shot

详见 docs/development-log.md

## 已知限制

- 最多验证两轮工具调用，未测三轮以上复杂链路
- 无会话记忆，每次请求独立
- 子 Agent 离线时降级返回错误提示，无重试队列
- 无用户认证、无限流、无压测

详见 docs/limitations.md
