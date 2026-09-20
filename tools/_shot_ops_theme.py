# -*- coding: utf-8 -*-
"""orchestrator /ops 页「换肤 + 工具调用链展开交互」的截图回归工具（可复用）。

设计要点（沿用 ecom 仓 tools/advisor_shot.py 已实测的坑）：
  · CDP 连本机 9224 的 Chromium；**独立 context**（不碰用户登录态）
  · 视口必须走 CDP `Emulation.setDeviceMetricsOverride`（`new_context(viewport=)` 在 CDP 上不生效）
    并**回读 innerWidth 校验**，对不上拒绝出图
  · 截图必须走 CDP `Page.captureScreenshot`（`page.screenshot()` 截的是真实窗口，不是仿真视口）
  · 输出 sha256 + 尺寸清单，供改前/改后逐屏比对

用法：
  python3 -X utf8 tools/_shot_ops_theme.py --out _evidence/ops-theme-<ts>/before
  python3 -X utf8 tools/_shot_ops_theme.py --out .../after --expand-all --click-first-session
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.request

CDP = 'http://127.0.0.1:9224'
FE = 'http://127.0.0.1:8010'
ROOT = pathlib.Path(__file__).resolve().parent.parent


def demo_admin_password() -> str:
    """从 README §4.2 演示账号表解析 admin 口令（不落盘、不打印）。"""
    txt = (ROOT / 'README.md').read_text(encoding='utf-8', errors='replace')
    m = re.search(r'^\|\s*`?admin`?\s*\|\s*`?([^|`]+?)`?\s*\|', txt, re.M)
    if not m:
        raise SystemExit('未能从 README §4.2 解析 admin 口令')
    return m.group(1).strip()


def login_cookie(base: str = FE) -> dict:
    body = json.dumps({'username': 'admin', 'password': demo_admin_password()}).encode()
    req = urllib.request.Request(base + '/auth/login', data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.headers.get_all('Set-Cookie') or []
    if not raw:
        raise SystemExit('登录未返回 Set-Cookie')
    c = raw[0].split(';')[0]
    name, _, val = c.partition('=')
    return {'name': name, 'value': val, 'domain': '127.0.0.1', 'path': '/'}


VIEWPORTS = {'desktop': {'width': 1440, 'height': 900},
             'mobile': {'width': 390, 'height': 844}}

SHOTS = [
    # (文件名, URL, 说明)
    ('ops-default', '/ops', '运行观测台 · 默认态（未展开任何链路）'),
    ('ops-expanded', '/ops', '运行观测台 · 展开第一轮调用链'),
    ('ops-all-expanded', '/ops', '运行观测台 · 展开全部轮次 + 全部步骤'),
    ('chat-default', '/', '客服对话页 · 默认态（换肤不得伤到它）'),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--viewport', default='desktop', choices=list(VIEWPORTS))
    ap.add_argument('--expand-all', action='store_true')
    ap.add_argument('--click-first-session', action='store_true')
    ap.add_argument('--cdp', default='', help='改用已运行的浏览器（如 http://127.0.0.1:9224）；默认自带 Chrome 实例')
    ap.add_argument('--base', default=FE, help='被测服务地址（默认 8010；验证阶段可用临时实例如 8011）')
    ap.add_argument('--wait', type=int, default=1400)
    a = ap.parse_args()

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    vp = VIEWPORTS[a.viewport]
    cookie = login_cookie(a.base)

    from playwright.sync_api import sync_playwright
    manifest = []
    with sync_playwright() as pw:
        # ⚠️ 2026-09-20 实测：用户的常驻 CDP Chrome（9224）会出现 `<ws connected>` 后不响应、
        #    连接超时 180s 的情况 —— 不依赖它；默认自己起一个 headless 实例（--no-proxy-server
        #    绕开本机系统代理对 127.0.0.1 的劫持）。
        if a.cdp:
            b = pw.chromium.connect_over_cdp(a.cdp)
            ctx = b.new_context()
            own = False
        else:
            b = pw.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            ctx = b.new_context()
            own = True
        ctx.add_cookies([cookie])
        pg = ctx.new_page()
        cdp = ctx.new_cdp_session(pg)
        cdp.send('Emulation.setDeviceMetricsOverride',
                 {'width': vp['width'], 'height': vp['height'], 'deviceScaleFactor': 1, 'mobile': True})
        try:
            for name, path, desc in SHOTS:
                if a.expand_all and name in ('ops-expanded', 'ops-all-expanded'):
                    pass  # 需要页面已加载
                pg.goto(a.base + path, wait_until='load')
                pg.wait_for_timeout(a.wait)
                real = pg.evaluate('() => ({w: innerWidth, h: innerHeight})')
                if real['w'] != vp['width']:
                    print(f'✗ 视口未生效：请求 {vp["width"]} 实际 {real["w"]} → 拒绝对比', flush=True)
                    return 3

                acted = ''
                if name == 'ops-expanded':
                    # 先全部收起，再展开第一轮（轮次 + 其步骤）—— 真正体现"展开这个动作"
                    pg.evaluate('''() => document.querySelectorAll('#detail details')
                                    .forEach(d => { d.open = false; })''')
                    pg.wait_for_timeout(200)
                    n = pg.evaluate('''() => {
                        const d = document.querySelector('#detail details.turn-card');
                        if (!d) return 0;
                        d.open = true;
                        d.querySelectorAll('details.step').forEach(s => { s.open = true; });
                        return 1; }''')
                    opened = pg.evaluate('''() => document.querySelectorAll('#detail details[open]').length''')
                    acted = f'收起→展开第一轮（轮 {n}，展开元素 {opened}）'
                    pg.wait_for_timeout(500)
                if name == 'ops-all-expanded':
                    n = pg.evaluate('''() => {
                        const cards = document.querySelectorAll('#detail details.turn-card');
                        cards.forEach(c => { c.open = true; });
                        const steps = document.querySelectorAll('#detail .step');
                        steps.forEach(s => { s.open = true; });
                        return steps.length; }''')
                    acted = f'全部展开（{n} 步）'
                    pg.wait_for_timeout(600)
                if name == 'ops-default' and a.click_first_session:
                    pg.evaluate('''() => { const r = document.querySelector('#sessList .row'); if (r) r.click(); }''')
                    pg.wait_for_timeout(700)
                    acted = '选中首个会话'

                raw = base64.b64decode(cdp.send('Page.captureScreenshot',
                                                 {'format': 'png', 'captureBeyondViewport': False})['data'])
                p = out / (name + '.png')
                p.write_bytes(raw)
                doc = pg.evaluate('() => ({h: document.documentElement.scrollHeight, '
                                  'n: document.querySelectorAll("#detail details").length})')
                manifest.append({'file': p.name, 'url': a.base + path, 'desc': desc, 'acted': acted,
                                 'viewport': vp, 'actual': real, 'page_h': doc['h'],
                                 'details': doc['n'],
                                 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
                print(f'  ✓ {p.name:22s} {len(raw):>7d}B  {desc} {acted}', flush=True)
        finally:
            ctx.close()
            if own:
                b.close()

    (out / 'manifest.json').write_text(json.dumps(
        {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'shots': manifest},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'清单：{out / "manifest.json"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
