"""
Supervisor 评估脚本。
加载 test_cases.json → 逐条调 run_supervisor_with_trace → 检查工具调用和关键词命中。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervisor import run_supervisor_with_trace

# ── 加载测试集 ────────────────────────────────────────

TEST_FILE = os.path.join(os.path.dirname(__file__), "test_cases.json")

with open(TEST_FILE, "r", encoding="utf-8") as f:
    test_cases: list[dict] = json.load(f)


# ── 评估函数 ──────────────────────────────────────────

def check_tools(expected: list[str], actual: list[str]) -> bool:
    """所有预期工具都被调用即为通过。"""
    return set(expected).issubset(set(actual))


def check_keywords(expected: list[str], answer: str) -> tuple[bool, list[str]]:
    """检查关键词是否出现在回答中。返回 (全命中, 未命中列表)。"""
    missed = [kw for kw in expected if kw not in answer]
    return len(missed) == 0, missed


# ── 逐条评估 ──────────────────────────────────────────

total = len(test_cases)
tool_correct = 0
keyword_correct = 0
results: list[dict] = []

for case in test_cases:
    msg = case["message"]
    exp_tools = case["expected_tools"]
    exp_kw = case["expected_keywords"]

    try:
        answer, actual_tools = run_supervisor_with_trace(msg)
    except Exception as e:
        answer = f"ERROR: {e}"
        actual_tools = []

    tool_ok = check_tools(exp_tools, actual_tools)
    kw_ok, kw_missed = check_keywords(exp_kw, answer) if exp_kw else (True, [])

    if tool_ok:
        tool_correct += 1
    if kw_ok:
        keyword_correct += 1

    results.append({
        "id": case["id"],
        "message": msg,
        "scenario": case["scenario"],
        "expected_tools": exp_tools,
        "actual_tools": actual_tools,
        "tool_ok": tool_ok,
        "expected_keywords": exp_kw,
        "kw_ok": kw_ok,
        "kw_missed": kw_missed,
        "answer_preview": answer[:120],
    })

    time.sleep(0.3)  # 避免 API 限流（Supervisor 多轮调用更慢）


# ── 分类别统计 ────────────────────────────────────────

scenario_groups: dict[str, list] = {}  # {label: [results]}
for r in results:
    # 从 scenario 提取大类
    prefix = r["scenario"].split("-")[0]
    if prefix not in scenario_groups:
        scenario_groups[prefix] = []
    scenario_groups[prefix].append(r)


# ── 报表 ──────────────────────────────────────────────

print("=" * 60)
print(f"  Supervisor 评估报告")
print(f"  总用例: {total}")
print(f"  工具调用准确率: {tool_correct}/{total} = {tool_correct/total*100:.1f}%")
print(f"  关键词命中率:   {keyword_correct}/{total} = {keyword_correct/total*100:.1f}%")
print("=" * 60)

print("\n--- 分类别 ---")
for label, items in scenario_groups.items():
    t_ok = sum(1 for r in items if r["tool_ok"])
    k_ok = sum(1 for r in items if r["kw_ok"])
    n = len(items)
    print(f"  {label} ({n}条): 工具={t_ok}/{n} ({t_ok/n*100:.0f}%)  关键词={k_ok}/{n} ({k_ok/n*100:.0f}%)")

# 失败案例
tool_fails = [r for r in results if not r["tool_ok"]]
kw_fails = [r for r in results if not r["kw_ok"]]

if tool_fails:
    print(f"\n--- 工具调用失败（{len(tool_fails)}条）---")
    for r in tool_fails:
        print(f"  [{r['id']}] {r['scenario']}")
        print(f"    消息: {r['message']}")
        print(f"    预期: {r['expected_tools']}  实际: {r['actual_tools']}")

if kw_fails:
    print(f"\n--- 关键词未命中（{len(kw_fails)}条）---")
    for r in kw_fails:
        print(f"  [{r['id']}] {r['scenario']}")
        print(f"    消息: {r['message']}")
        print(f"    未命中: {r['kw_missed']}")
        print(f"    回答: {r['answer_preview']}...")
