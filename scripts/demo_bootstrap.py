"""一键 demo 的密钥准备（幂等）—— 确保三个仓库共用同一个 AUTH_SECRET。

为什么需要这个脚本
    编排层签发 JWT，下游服务各自用同一个 AUTH_SECRET 验签 —— 所以三仓密钥必须一致。
    fail-closed 之后，密钥没配好服务会直接拒绝启动；这个脚本负责把密钥准备好。

行为（幂等，可反复运行）
    三仓都没有 AUTH_SECRET        → 生成一次随机密钥，写入三仓 .env
    三仓都有且完全一致            → 什么都不做
    三仓都有但不一致 / 只有部分有  → 报错退出（不悄悄改动；附对齐办法）

用法：
    python scripts/demo_bootstrap.py            # 检查/生成
    python scripts/demo_bootstrap.py --force-sync   # 以 orchestrator-agent 的值为准，覆盖另两仓
    python scripts/demo_bootstrap.py --rotate       # 强制重新生成（会让已登录会话失效）
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

# 本脚本位于 <根>/orchestrator-agent/scripts/ → 三仓的公共父目录
BASE = Path(__file__).resolve().parent.parent.parent
REPOS = ["orchestrator-agent", "customer-service-agent", "rag-agent"]
ENV_KEY = "AUTH_SECRET"
PLACEHOLDER = "dev-only-insecure-secret-change-me"


def fingerprint(value: str) -> str:
    """只打印指纹，不打印密钥本身。"""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def set_env_key(path: Path, key: str, value: str) -> None:
    """就地设置一个键，保留文件里其它内容与注释顺序。"""
    if not path.exists():
        example = path.parent / ".env.example"
        if example.exists():
            shutil.copy2(example, path)
        else:
            path.write_text("", encoding="utf-8")

    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    line = f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-sync", action="store_true",
                    help="三仓不一致时，以 orchestrator-agent 的值为准覆盖另两仓")
    ap.add_argument("--rotate", action="store_true",
                    help="强制重新生成密钥（已登录会话会失效）")
    args = ap.parse_args()

    print("=" * 70)
    print("  三仓共享密钥准备（AUTH_SECRET）")
    print("=" * 70)

    missing_dirs = [r for r in REPOS if not (BASE / r).is_dir()]
    if missing_dirs:
        print(f"✗ 找不到仓库目录：{missing_dirs}")
        print(f"  三个仓库需要克隆到同一个父目录下：{BASE}")
        return 1

    envs = {r: read_env(BASE / r / ".env") for r in REPOS}
    values = {r: (envs[r].get(ENV_KEY) or "").strip() for r in REPOS}

    for r in REPOS:
        v = values[r]
        state = "未配置" if not v else ("占位值/默认值" if v == PLACEHOLDER else f"已配置（指纹 {fingerprint(v)}）")
        print(f"  {r:24s} {state}")

    if args.rotate:
        secret = secrets.token_urlsafe(48)
        for r in REPOS:
            set_env_key(BASE / r / ".env", ENV_KEY, secret)
        print(f"\n✓ 已重新生成并写入三仓（指纹 {fingerprint(secret)}）")
        return 0

    configured = [r for r in REPOS if values[r] and values[r] != PLACEHOLDER]
    unset = [r for r in REPOS if not values[r]]

    if not configured and unset:
        secret = secrets.token_urlsafe(48)
        for r in REPOS:
            set_env_key(BASE / r / ".env", ENV_KEY, secret)
        print(f"\n✓ 三仓都没有密钥 → 已生成一次随机密钥并写入三个 .env（指纹 {fingerprint(secret)}）")
        print("  注意：.env 在 .gitignore 里，不会入库；密钥只存在本机。")
        return 0

    if len(configured) == len(REPOS) and len({values[r] for r in configured}) == 1:
        print(f"\n✓ 三仓密钥一致（指纹 {fingerprint(values[configured[0]])}），无需改动")
        return 0

    # 不一致 / 部分配置
    if args.force_sync:
        source = values["orchestrator-agent"] or (values[configured[0]] if configured else "")
        if not source:
            print("\n✗ --force-sync 需要至少一个仓库已有密钥，当前一个都没有。")
            return 1
        for r in REPOS:
            if values[r] != source:
                set_env_key(BASE / r / ".env", ENV_KEY, source)
                print(f"  → 已把 {r} 对齐到指纹 {fingerprint(source)}")
        print(f"\n✓ 三仓已统一（指纹 {fingerprint(source)}）")
        return 0

    print("\n✗ 三仓的 AUTH_SECRET 不一致（或只有部分仓库配置了）。")
    print("  三个服务必须共用同一个密钥，否则跨服务验签会全部 401。")
    for r in REPOS:
        print(f"    {r:24s} {'（空）' if not values[r] else '指纹 ' + fingerprint(values[r])}")
    print("\n  对齐办法（任选）：")
    print("    python scripts/demo_bootstrap.py --force-sync   # 以 orchestrator-agent 的值为准覆盖另两仓")
    print("    python scripts/demo_bootstrap.py --rotate       # 重新生成一个新的三仓共享密钥")
    return 1


if __name__ == "__main__":
    sys.exit(main())
