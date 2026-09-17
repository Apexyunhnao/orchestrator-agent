# 开发日志

## 2026-08-08 — 分类器迭代

### 错误 1：模型名无效 → 全部空返回

**现象**：使用 `deepseek-v4-flash`，55 条测试 38.2% 准确率，knowledge 100% / service 0% / mixed 0%。

**根因**：该模型名在 DeepSeek API 不存在。API 未报错，静默返回空 content → 归一化兜底全部判 knowledge。

**教训**：写代码前先查官方文档确认模型名，不要凭记忆。两个已有项目（customer-service-agent、rag-agent）都用的 `deepseek-v4-pro`，只有本项目一开始用错了。

**解决**：全局替换为 `deepseek-v4-pro`。

### 错误 2：max_tokens 太小 → 截断为空

**现象**：`max_tokens=10`，LLM 返回被截断成空字符串。逐步调到 50→100→200 才稳定。

**根因**：DeepSeek 有时会先输出简短解释再给答案（如"该消息涉及订单操作，应归类为 service"），10 token 连解释都放不下就被截断。

**教训**：分类任务的 max_tokens 不能按"只需一个词"来设最小值。要留 token 给模型可能的解释输出。200 是安全值。

### 错误 3：prompt 偏置 → 退差价误判

**现象**：ORC-035 "这个商品买贵了能不能退差价" 判为 knowledge，ORC-044 "退差价有时间限制吗？我上周买的现在降价了" 判为 knowledge（实际应 service / mixed）。

**根因**：system prompt 没明确覆盖"疑问句式但隐含业务操作意图"的情况。模型看"能不能""有无限制"就归类为知识查询。

**教训**：
- 分类 prompt 不只是定义类别，还要覆盖口语化/隐性意图的表达方式
- "退差价/退款/退货/换货"类消息，即使句式为疑问句，用户真实意图也是操作不是提问
- prompt 需要加一条："售后类诉求（退差价、退款、退货、换货），即使以疑问句式表达，也优先判 service 或 mixed"

### 错误 4：reasoning 模型不适合分类任务

**现象**：切到 `deepseek-v4-pro` 后，55 条全部空返回（0%）。

**根因**：v4-pro 是 reasoning 模型（类似 R1），调用时会在 `reasoning_content` 里做内部推理，把 token 预算全烧在思考过程上，真正的 `content` 被截断为空。

**教训**：分类、路由等轻量任务不能用 reasoning 模型。非推理模型（deepseek-chat → v4-flash）够用，又快又便宜。reasoning 模型只在复杂推理、多步决策时才有价值。

### 最终结果

| 版本 | 准确率 | 关键改动 |
|------|--------|----------|
| v1 | 38.2% | deepseek-v4-flash 模型名不存在，全空 |
| v2 | 0% | deepseek-v4-pro（reasoning），全空 |
| v3 | 92.7% | deepseek-chat + max_tokens=200 + 强化 prompt |
| v4 | 98.2% | 加 few-shot examples，knowledge+service 双100% |

唯一失败：ORC-043（标注争议，模型判 service 也合理）。

---

## 2026-08-08 (晚) — 架构重做：从路由到 Supervisor

### 为什么重做

v1 架构本质是 HTTP 路由器：分类→转发→返回。两个致命问题：

1. **业务不搭**：RAG 查年假、客服查快递，两个 Agent 服务不同场景，硬拼在一起没有真实业务闭环
2. **编排深度为零**：一轮分类→转发，多 Agent 之间没有上下文传递、没有多步协作

**决策**：RAG 知识库换成客服政策文档（退货/退款/换货/物流），架构改为 LangGraph Supervisor 模式。

### 错误 5：httpx 走系统代理 → localhost 被拦截

**现象**：编排器调 RAG（localhost:8002）始终返回 503，但 curl 直接调 200 OK。

**根因**：Windows 系统代理（proxy）拦截了 httpx 请求。响应头显示 `proxy-connection: close`，代理拒绝转发 localhost 请求。

**教训**：Windows 环境下 httpx 默认 `trust_env=True`，会走系统代理。调 localhost 服务必须设 `trust_env=False`。

**解决**：tools.py 改用 `httpx.Client(trust_env=False)`。

### Supervisor 评估结果

> **口径更新（2026-09-13）**：下表是 v1 时期 30 条测试集的结果，仅作历史记录。
> 当前测试集为 32 条，最新结果 **工具调用 31/32 = 96.9%、关键词命中 17/32 = 53.1%**，
> 以 README.md / `eval/results.json` 为准。

| 指标 | 数值（v1 时期 30 条测试集） |
|------|------|
| 工具调用准确率 | 29/30 = 96.7% |
| 唯一失败 | S18 "公司规定退货运费谁出？我有个单子要退" — 无显式订单号，LLM未调 create_ticket |
| 端到端验证 | 纯政策/纯业务/混合 三个场景全通 |

### 与旧架构对比

| | v1 路由 | v2 Supervisor |
|---|--------|-------------|
| 架构 | 一次分类→转发 | 多轮决策→工具调用→再决策 |
| LLM 调用 | 1 次 | 3-5 次 |
| 上下文传递 | 无 | 子Agent结果送回Supervisor继续决策 |
| 多步协作 | 不支持 | 先RAG查政策→再客服建工单 |
| 实现深度 | "就是个if/else" | "LangGraph状态图+循环决策" |

---

## 2026-08-08 (晚) — 全链路验证 + 工具做实 + Docker 化

### 全链路端到端测试

编写 `e2e_fullstack.py`：启动 mock RAG (:8002) + 真实客服 Agent (:8001)，Supervisor 通过 HTTP 调两个子 Agent。

三个场景验证通过：
- 纯政策查询：Supervisor → query_rag → RAG返回退货政策 → 回答（7.0s）
- 纯业务操作：Supervisor → create_ticket → 客服建工单 API-xxx → 回答（13.9s）
- 混合场景：Supervisor → query_rag + create_ticket 并行 → 综合回答（34.8s）

**教训**：Windows 上 httpx 默认走系统代理，调 localhost 返回 503。`trust_env=False` 解决。

### 客服 Agent 工具做实

`update_address` 和 `update_remark` 从 mock（只返回成功消息）改为真实 SQL 写操作：
- `update_address` → `UPDATE customers SET address = ? WHERE id = ?`
- `update_remark` → 新建 `order_remarks` 表 + `INSERT INTO order_remarks`

涉及资金的操作（退款/退差价/换货）保持 mock 安全边界：数据查询是真实 SQL JOIN，地址和备注是真实写入，资金操作做了安全限制。

### 客服 Agent 模型切换

nodes.py 从 `deepseek-v4-pro` 切到 `deepseek-chat`：
- 原因：v4-pro 是 reasoning 模型，tool-calling 场景 token 被 reasoning_content 占满
- 结果：分类 100% 不变，耗时 713s → 184s（4x），行动准确率 96.6%

### RAG Agent 测试用例重写

旧 24 条测试用例针对人事类文档（员工手册/IT FAQ 等），文档已换为电商政策但测试未更新。重写为 20 条电商政策测试 + 分层留出集（每文档至少 1 条 holdout）。

### Docker 化

三个服务各自 Dockerfile + 根目录 docker-compose.yml，`docker-compose up -d` 一键启动。
- RAG Dockerfile：含自动 ingest + embedding 模型预下载
- 客服 Dockerfile：volume 持久化 data/ 目录
- 编排器 Dockerfile：依赖 rag 和 ticket 的健康检查

## 2026-09-16 — 流式输出接入（编排器）

### 错误 1：`streaming=True` 加了却收不到任何 token 事件

**现象**：给 LLM 开 `streaming=True`、用 `astream_events(version="v2")` 订阅，事件里只有 `on_chain_start/end` 和 `on_tool_start/end`，**一个 `on_chat_model_stream` 都没有**。

**根因**：不是 streaming 的问题，是 **callbacks 没传进节点**。LangGraph 的同步节点跑在线程池里，事件回调靠 `contextvar` 传播，跨线程不复制 → 节点内部的 LLM 调用成了黑盒。
三个变体对照实测：同步 `invoke`（0 事件）、async 节点 `ainvoke`（**也是 0，反直觉**）、`def node(state, config)` + `invoke(msgs, config=config)`（**271 个 token chunk**）。只有第三种有效。

**教训**：只要想在节点外面观测到节点内部的调用，节点签名必须显式接收 `config` 并透传。别指望 contextvar 自动传播——尤其是同步节点。

### 错误 2：决策轮的正文会漏给用户

**现象**：p1 的 `supervisor_node` 同一个函数既决策又汇总。原以为"决策轮 content 为空"，实测决策轮**先吐一句英文**（`"I'll help you with both the return policy question..."`，87 字）再给 tool_calls。直接流式会把这句英文推给用户。

**根因**：判别依据选错了——不能用 `content` 是否为空，要用 `tool_call_chunks`。

**解决**：① 在 prompt 里加输出格式硬约束（"调用工具那一轮不要输出任何文字"），加完决策轮 content 归零；② 流式层按 `tool_call_chunks` 分型，决策轮整轮静默；③ 保留 `revoke` 兜底事件。实测加约束后**首个 chunk 就能判型**（决策轮 3/3 次全是 TTTTTT，汇总轮全是 CCCCCC）。

**教训**：ReAct 类循环里"哪一轮该给用户看"这个判断，永远要看**结构化信号**（tool_calls），不要看自然语言内容。

### 错误 3：`metadata.langgraph_node` 会被子 chain 继承 → steps 多记

**现象**：流式版收集的 `steps` 比老版多出 2 条（5 条 vs 3 条）。

**根因**：`metadata.langgraph_node` 会一路继承到节点内部的 `ChatOpenAI` RunnableBinding chain 上，用它过滤 `on_chain_end` 会把内部调用也当成节点记一笔。

**解决**：收集节点步骤时按 **chain 的 `name`**（`"supervisor"` / `"tools"`）过滤；`langgraph_node` 只用来过滤 chat model 事件。改完与老版 steps 结构完全一致。

### 回归验证

改完跑 `eval/run_eval.py`：工具 **31/32 = 96.9%**、关键词 **17/32 = 53.1%**，与 9-10 基线**完全一致**（prompt 加约束、LLM 开 streaming 都没打坏原有行为）。

完整实现记录 + 探针脚本见 `docs/20260916_流式输出实现记录.md` 与 `docs/probes/20260916_stream/`。
