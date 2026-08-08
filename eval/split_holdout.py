"""
留出集切分脚本。
将 test_cases.json 按 8:2 切分为 train 和 holdout，固定随机种子。
"""

import json
import os
import random

TEST_FILE = os.path.join(os.path.dirname(__file__), "test_cases.json")
TRAIN_FILE = os.path.join(os.path.dirname(__file__), "train_cases.json")
HOLDOUT_FILE = os.path.join(os.path.dirname(__file__), "holdout_cases.json")
SEED = 42
SPLIT_RATIO = 0.8

# 加载
with open(TEST_FILE, "r", encoding="utf-8") as f:
    cases: list[dict] = json.load(f)

# 切分
random.seed(SEED)
shuffled = cases.copy()
random.shuffle(shuffled)

split_idx = int(len(shuffled) * SPLIT_RATIO)
train = shuffled[:split_idx]
holdout = shuffled[split_idx:]

# 保存
for path, data in [(TRAIN_FILE, train), (HOLDOUT_FILE, holdout)]:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 分布统计
print(f"总用例: {len(cases)}")
print(f"train: {len(train)} 条")
print(f"holdout: {len(holdout)} 条")
print()

for label in ("knowledge", "service", "mixed"):
    train_count = sum(1 for c in train if c["expected_intent"] == label)
    holdout_count = sum(1 for c in holdout if c["expected_intent"] == label)
    print(f"  {label}: train={train_count}, holdout={holdout_count}")
