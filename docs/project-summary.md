# 项目总结 — RAG → 客服 Agent → 多 Agent 编排

> 最后更新：2026-08-08

## 项目一：RAG 知识库问答 Agent（rag-agent）

**路径**：E:\BCRJ\vsxiangmu\rag-agent

**技术栈**：ChromaDB + BAAI/bge-small-zh-v1.5 + DeepSeek + FastAPI + SQLite

**做什么**：加载文档 → 分块 → 向量化 → 入库。用户自然语言提问，检索 Top-K 片段，LLM 合成带来源引用的回答。

**文档**：4 份客服政策（退货/退款/换货/物流配送），共 ~86 行，8 个 chunk。

**评估**：20 条测试用例（2026-08-08 重写，匹配新文档），三指标 + 分层留出集：
- 检索命中率（Top-5 中包含目标文档）
- 回答准确率（至少 1 个关键词命中）
- 来源正确率（[来源: xxx] 与目标文档一致）
- 留出集：16 train + 4 holdout（seed=42，每文档至少 1 条）

**Docker**：Dockerfile 含自动 ingest + 预下载 embedding 模型。

**面试要点**：检索管道（chunk_size=300/chunk_overlap=50/cosine）、向量库选型（ChromaDB 持久化）、引用溯源、评估三指标、留出集防过拟合。

---

## 项目二：客服工单处理 Agent（customer-service-agent）

**路径**：E:\BCRJ\vsxiangmu\customer-service-agent

**技术栈**：LangGraph + DeepSeek-chat + FastAPI + SQLite × 2

**做什么**：接收客服工单消息 → 意图分类（订单/物流/售后）→ 调对应工具 → 自动处理或转人工。8 个工具：查订单、查物流、改地址、退差价、加备注、催派送、退�款、换货。安全边界写死在工具函数里。

**数据库**：
- `data/business.db` — 业务数据（customers 20、orders 30、logistics 30，外键约束）
- `data/tickets.db` — 工单记录

**真实写操作**（2026-08-08）：
- `update_address` → `UPDATE customers SET address = ?` 真实写库
- `update_remark` → `INSERT INTO order_remarks` 真实写库
- 涉及资金的操作（退款/退差价/换货）保持 mock 安全边界

**模型**：deepseek-chat（从 v4-pro 切过来，原因：reasoning 模型在 tool-calling 场景 token 被 reasoning_content 占满，真正输出截断为空。切 chat 后分类 100% 不变，耗时 713s→184s，4x 提升）

**评估**：59 条测试集，8:2 分层留出。结果：
- 分类准确率 100%
- 行动准确率 96.6%
- 自动处理成功率 92.0%
- 物流类 100%

**Docker**：Dockerfile，`data/` 目录 volume 持久化。

**面试要点**：LangGraph 状态图（classify→handle→decide→auto/escalate→reply）、三层防护（工具边界→风险规则→人工兜底）、SQLite 体现数据库设计能力（外键、JOIN、参数化查询）、模型选型教训（reasoning vs chat）。

---

## 项目三：多 Agent 编排 Supervisor（orchestrator-agent）

**路径**：E:\BCRJ\vsxiangmu\orchestrator-agent

**技术栈**：LangGraph（Supervisor 模式）+ LangChain + DeepSeek-chat + FastAPI + SQLite + httpx

**做什么**：Supervisor Agent 作为总控，协调 RAG Agent（查政策）和客服 Agent（办业务），多轮逐步决策。

**两版迭代**：

v1（HTTP 路由器）— 已废弃
- classifier.py 分类 + router.py 转发
- 四个模型坑：不存在→reasoning 全空→max_tokens 截断→收敛
- 推到重做原因：架构本质是 if/else，没有真正的编排

v2（Supervisor）— 当前版本
- supervisor.py：LangGraph 两节点（supervisor + tools），条件边 + 循环边
- tools.py：query_rag 和 create_ticket 包装为 LangChain Tool
- 30 条多轮测试，工具调用准确率 96.7%
- gateway.db：请求日志 + GET /stats 统计接口

**端到端验证**（2026-08-08 新增）：
- 启动 mock RAG (:8002) + 真实客服 Agent (:8001) → Supervisor 全链路调用
- 纯政策查询 ✓ | 纯业务操作 ✓ | 混合场景 ✓
- 脚本：`e2e_fullstack.py`

**Docker**：Dockerfile + docker-compose.yml（三服务一键启动）

**面试要点**：Supervisor 模式 vs 路由（多轮决策 vs 一次分类）、LangGraph 状态图实现、多 Agent HTTP 解耦、模型选择教训（三个模型名全错→收敛）、httpx Windows 代理坑。

---

## 三个项目的逻辑链

```
项目一 RAG ──→ 检索管道能力（向量化 + 检索 + 生成 + 留出集评估）
                     ↓
项目二 客服 Agent ──→ 工具调用 + 安全边界（8 工具 + 三层防护 + SQLite）
                     ↓
项目三 Supervisor ──→ 多 Agent 编排（协调 RAG 和客服，多轮决策 + 全链路验证）
```

**评估体系统一**：三个项目均采用 8:2 留出集 + seed=42 + 分层抽样，杜绝"对着答案调"的质疑。

**数据库设计**：三个 SQLite（queries.db / business.db+tickets.db / gateway.db），体现全链路持久化意识。

**模型迭代叙事**：deepseek-v4-flash（不存在）→ v4-pro（reasoning，全空）→ deepseek-chat（收敛，98.2%/96.6%），用实际数据讲清楚推理模型和普通模型的区别。

**Docker 化**：三服务独立 Dockerfile + docker-compose 一键编排，从"三个终端手动启动"升级为 `docker-compose up -d`。

面试叙事：从单 Agent 到多 Agent 到编排，三个项目覆盖 Agent 应用开发全栈——检索、工具调用、安全边界、评估体系、多 Agent 协调、数据库设计、容器化。每个项目有独立的测试集、留出集和指标。
