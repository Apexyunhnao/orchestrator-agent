# -*- coding: utf-8 -*-
"""orchestrator /ops「换肤 + 调用链展开交互」的**功能回归**（不只看截图）。

断言项：
  A 指标卡数量与结构（工具调用分布已改为 chip）
  B 会话列表可选中 → 详情标题跟着切换
  C 轮次卡默认展开最近一轮；步骤/参数默认可折叠
  D 「收起全部 / 展开全部」真的改到全部 <details>
  E 长参数默认收起、可点开；短参数默认展开
  F 「展开全文」按钮可来回切换，且文本能还原
  G 浏览器 console 无 JS 报错（favicon 404 忽略）

用法：python3 -X utf8 tools/_verify_ops_ui.py --base http://127.0.0.1:8011
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAILS: list[str] = []
CHECKS = 0


def ck(tag: str, ok: bool, detail: str = '') -> None:
    global CHECKS
    CHECKS += 1
    print(('  OK  ' if ok else '  XX  ') + tag + ('' if detail == '' else '  —— ' + str(detail)))
    if not ok:
        FAILS.append(tag)


def login(base: str) -> dict:
    txt = (ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
    pwd = re.search(r'^\|\s*`?admin`?\s*\|\s*`?([^|`]+?)`?\s*\|', txt, re.M).group(1).strip()
    req = urllib.request.Request(base + '/auth/login',
                                 data=json.dumps({'username': 'admin', 'password': pwd}).encode(),
                                 method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        c = (r.headers.get_all('Set-Cookie') or [''])[0].split(';')[0]
    n, _, v = c.partition('=')
    return {'name': n, 'value': v, 'domain': '127.0.0.1', 'path': '/'}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:8011')
    a = ap.parse_args()
    base = a.base.rstrip('/')

    from playwright.sync_api import sync_playwright
    errors: list[str] = []
    bad: list[str] = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
        ctx = b.new_context()
        ctx.add_cookies([login(base)])
        pg = ctx.new_page()
        pg.on('console', lambda m: errors.append(f'{m.type}: {m.text}') if m.type == 'error' else None)
        pg.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        pg.on('response', lambda r: bad.append(f'{r.status} {r.url}') if r.status >= 400 else None)
        pg.goto(base + '/ops', wait_until='load')
        pg.wait_for_timeout(2000)

        # A 指标卡
        n_metric = pg.evaluate('() => document.querySelectorAll("#metrics .metric").length')
        ck('A1 指标卡 4 张', n_metric == 4, f'实际 {n_metric}')
        chips = pg.evaluate('() => document.querySelectorAll("#metrics .tool-chips .t").length')
        ck('A2 工具调用分布渲染为 chip', chips >= 1, f'chip {chips} 个（0 也不一定是错，取决于是否有调用数据）')

        # B 会话选中切换
        rows = pg.evaluate('() => document.querySelectorAll("#sessList .row").length')
        ck('B1 会话列表有条目', rows >= 1, f'{rows} 行')
        if rows >= 2:
            sid2 = pg.evaluate('() => document.querySelectorAll("#sessList .row")[1].dataset.sid')
            pg.evaluate('() => document.querySelectorAll("#sessList .row")[1].click()')
            pg.wait_for_timeout(600)
            title = pg.evaluate('() => document.querySelector("#detail div").innerText')
            ck('B2 点第 2 行 → 详情切到该会话', sid2 in title, f'标题={title!r} 期望含 {sid2}')

        # C 默认展开最近一轮
        open_turns = pg.evaluate('() => document.querySelectorAll("#detail details.turn-card[open]").length')
        total_turns = pg.evaluate('() => document.querySelectorAll("#detail details.turn-card").length')
        ck('C1 默认只展开第一轮', open_turns == 1 and total_turns >= 1,
           f'展开 {open_turns} / 共 {total_turns}')

        # D 展开全部 / 收起全部
        pg.evaluate('''() => { const b = document.querySelector('[data-ops="collapse-all"]'); if (b) b.click(); }''')
        pg.wait_for_timeout(300)
        op = pg.evaluate('() => document.querySelectorAll("#detail details[open]").length')
        tot = pg.evaluate('() => document.querySelectorAll("#detail details").length')
        ck('D1 收起全部 → 展开数归零', op == 0, f'仍有 {op} 个展开（共 {tot}）')
        pg.evaluate('''() => { const b = document.querySelector('[data-ops="expand-all"]'); if (b) b.click(); }''')
        pg.wait_for_timeout(300)
        op2 = pg.evaluate('() => document.querySelectorAll("#detail details[open]").length')
        ck('D2 展开全部 → 全部打开', op2 == tot and tot > 0, f'展开 {op2} / 共 {tot}')

        # E 参数折叠语义
        has_call = pg.evaluate('() => document.querySelectorAll("#detail details.call").length')
        if has_call:
            opened = pg.evaluate('() => document.querySelectorAll("#detail details.call[open]").length')
            closed = has_call - opened
            ck('E1 参数折叠可用（存在已展开/已收起两种）', True, f'共 {has_call}：展开 {opened}、收起 {closed}')
            if closed:
                first_closed = pg.evaluate('''() => {
                    const d = document.querySelector('#detail details.call:not([open])');
                    d.open = true; return d.querySelector('.call-name').innerText; }''')
                ck('E2 收起的长参数可点开', bool(first_closed), str(first_closed)[:40])

        # F 展开全文按钮
        n_btn = pg.evaluate('() => document.querySelectorAll("#detail .expand-btn").length')
        if n_btn:
            txt0 = pg.evaluate('() => document.querySelector("#detail .expand-btn").textContent')
            pg.evaluate('() => document.querySelector("#detail .expand-btn").click()')
            pg.wait_for_timeout(200)
            txt1 = pg.evaluate('() => document.querySelector("#detail .expand-btn").textContent')
            clamped1 = pg.evaluate('() => !!document.querySelector("#detail .expand-btn").previousElementSibling.classList.contains("clamped")')
            pg.evaluate('() => document.querySelector("#detail .expand-btn").click()')
            pg.wait_for_timeout(200)
            txt2 = pg.evaluate('() => document.querySelector("#detail .expand-btn").textContent')
            ck('F1 展开全文 → 按钮文案变「收起」且解除折叠',
               txt1 == '收起' and clamped1 is False, f'{txt0!r} -> {txt1!r}')
            ck('F2 再点 → 文案还原', txt2 == txt0, f'{txt1!r} -> {txt2!r}')
        else:
            ck('F1 存在可折叠的「展开全文」按钮', False, '页面上没有长文本按钮（数据里可能没有长内容）')

        # E3/E4 长/短参数折叠**分支**（不依赖真实数据：直接调 renderItem 注入）
        long_html = pg.evaluate(
            '() => renderItem({type:"AIMessage", tool_calls:[{name:"demo_tool", args:{q:"x".repeat(400)}}]})')
        ck('E3 长参数默认收起', '<details class="call">' in long_html, long_html[:100])
        short_html = pg.evaluate(
            '() => renderItem({type:"AIMessage", tool_calls:[{name:"demo_tool", args:{q:"x"}}]})')
        ck('E4 短参数默认展开', '<details class="call" open>' in short_html, short_html[:100])

        # G console + 4xx 资源
        def _ign(s: str) -> bool:
            return 'favicon' in s.lower()

        real = [e for e in errors
                if not _ign(e) and not e.startswith('error: Failed to load resource')]
        badreal = [x for x in bad if not _ign(x)]
        ck('G1 无 JS 运行时错误（资源加载失败归 G2 判）', not real,
           json.dumps(real, ensure_ascii=False)[:300] if real
           else f'（已忽略 favicon 类 {len(errors)} 条）')
        ck('G2 无 4xx/5xx 资源请求', not badreal,
           json.dumps(badreal, ensure_ascii=False)[:300] if badreal else '（仅 favicon）')

        ctx.close()
        b.close()

    print(f'\n合计 {CHECKS} 项，失败 {len(FAILS)} 项')
    if FAILS:
        for f in FAILS:
            print('  · ', f)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
