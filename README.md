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

GET /health → 聚合三个服务状态
```json
{"orchestrator":"ok","rag":"ok","ticket":"ok"}
```

GET /stats
```json
{"total_requests": 42, "recent_10": [...], "tool_usage": {"query_rag": 30, "create_ticket": 22}}
```

## 输入校验

- 空消息 → 400
- 超长（>500 字）→ 400
- 纯英文/纯符号 → 400
- 正常中文 → 通过

## 评估

**测试集**：32 条场景，覆盖纯政策查询（7）/ 纯业务（7）/ 混合任务（7）/ 边界模糊（9）/ 三轮链路（1）/ 上下文传递（1）。
**数字来源**：`eval/results.json`（由 `eval/run_eval.py` 自动生成，README 与简历只引用它，禁止手写）。

| 指标 | 数值 | 口径说明 |
|------|------|---------|
| 工具调用准确率 | **31/32 = 96.9%** | 期望工具集合被实际调用（子集判定，即"覆盖预期工具调用"，多调不判错） |
| 边界模糊场景 | 8/9 = 88.9% | 模糊 / 口语化 / 情绪化 / 缺订单号的输入下仍正确路由 |
| 关键词命中率 | 17/32 = 53.1% | 最终回答是否包含用例预设关键词（口径说明见下） |
| 三轮链路验证 | S31 ✅ | 查政策 → 建工单 → 查物流 |
| 上下文传递验证 | S32 ✅ | RAG 答案传入客服 Agent 继续决策 |
| 降级验证 | ✅ | RAG 离线 → 返回"知识库暂不可用"，不阻断主流程 |

> **关键词命中率为什么只有 53.1%**：该指标检查最终回答里是否出现用例预设的关键词。但**边界模糊**场景（如"退款退款退款""我买的东西怎么还没到"）的正确行为就是**转人工**或**建工单由业务侧补全信息**，回答形如"已转人工，原因：…"，天然不含政策原文关键词——这类**安全的正确行为**会被该指标判为未命中。所以它只作参考，**核心指标是工具调用准确率**。

### 模型适配记录（2026-09，一次真实的回归定位）

DeepSeek 后端把 `deepseek-chat` 别名切到新模型后，边界模糊场景下模型变得保守（不调工具、直接文字回答），工具调用准确率从 96.9% 掉到 84.4%（27/32）。定位过程：

1. 换两代依赖（langchain 1.4.x 与 0.3.x）做对照 → 分数与**失败用例完全一致**（都是 S24/S25/S27/S28/S30）→ 排除依赖漂移
2. 绕开框架单独调 LLM → `tool_calls` 返回完全正常 → 排除工具绑定问题
3. 结论：模型换代带来的 tool-calling 行为漂移

修复：在 system prompt 里补"工具优先原则"（表述模糊/口语化/缺订单号时也要先调工具去查去办，别在没查前就反问或下结论），工具准确率**恢复到 96.9%**。

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

- 三轮以上工具调用已验证，但未测试并行调度场景
- 无会话记忆，每次请求独立
- /health 检测 + 降级返回错误提示，无自动重试队列
- 无用户认证、无限流、无压测

详见 docs/limitations.md
