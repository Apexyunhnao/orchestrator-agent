"""探针11：用 CDP 真跑 p1 前端，验证流式渲染 + 收集 JS 错误。
只读浏览器侧，不改项目文件。
"""
import base64
import json
import os
import tempfile
import time
import urllib.request

import websocket  # websocket-client

PORT = 9224
URL = "http://127.0.0.1:8010/"
QUESTION = "退货要什么条件？帮我把 ORD-1003 退了"
SHOT = os.path.join(tempfile.gettempdir(), "p1_stream_shot.png")

logs = []
errors = []


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


_id = 0


def cmd(ws, method, **params):
    global _id
    _id += 1
    my_id = _id
    ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == my_id:
            return msg
        collect(msg)


def collect(msg):
    m = msg.get("method")
    if m == "Runtime.consoleAPICalled":
        txt = " ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"].get("args", []))
        logs.append(f"[console.{msg['params']['type']}] {txt}")
        if msg["params"]["type"] == "error":
            errors.append(txt)
    elif m == "Runtime.exceptionThrown":
        d = msg["params"]["exceptionDetails"]
        errors.append(f"[exception] {d.get('text')} {d.get('exception', {}).get('description', '')}")
    elif m == "Log.entryAdded":
        e = msg["params"]["entry"]
        logs.append(f"[{e['level']}] {e['text']}")
        if e["level"] == "error":
            errors.append(e["text"])


def ev(ws, expr):
    r = cmd(ws, "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    res = r.get("result", {})
    if "exceptionDetails" in res:
        return f"<JS异常: {res['exceptionDetails'].get('text')}>"
    return res.get("result", {}).get("value")


def main():
    tab = http_json(f"/json/new?{URL}", method="PUT")
    print("新标签页:", tab.get("id"))
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    cmd(ws, "Page.enable")
    cmd(ws, "Runtime.enable")
    cmd(ws, "Log.enable")
    time.sleep(2.5)   # 等页面加载

    print("页面标题:", ev(ws, "document.title"))
    print("元素就绪:", ev(ws, "[!!document.getElementById('thread'), !!document.getElementById('msgInput'), "
                              "!!document.getElementById('sendBtn')]"))
    print("发送按钮文字:", ev(ws, "document.getElementById('sendBtn').textContent"))

    # 输入并点击发送
    print("\n--- 发送问题 ---")
    ev(ws, f"document.getElementById('msgInput').value = {json.dumps(QUESTION, ensure_ascii=False)};")
    ev(ws, "document.getElementById('sendBtn').click(); 'clicked'")
    print("点击后按钮文字:", ev(ws, "document.getElementById('sendBtn').textContent"))

    # 轮询最后一条回复的长度变化 → 验证是否逐步增长（真流式）
    samples = []
    t0 = time.time()
    for _ in range(60):
        time.sleep(0.25)
        n = ev(ws, "(function(){var b=document.querySelectorAll('.turn.bot .turn-body');"
                   "return b.length? b[b.length-1].textContent.length : -1;})()")
        btn = ev(ws, "document.getElementById('sendBtn').textContent")
        samples.append((round(time.time() - t0, 2), n, btn))
        if btn == "发送" and n and n > 0:
            break

    print("\n长度时间序列（秒，字数，按钮）:")
    for s in samples[::2][:24]:
        print(f"   {s[0]:5.2f}s  {s[1]:>5} 字  btn={s[2]}")

    growth_steps = sum(1 for i in range(1, len(samples)) if samples[i][1] > samples[i - 1][1])
    print(f"\n字数增长的采样点次数 = {growth_steps}（>3 说明是逐字流出，不是一次性出现）")

    print("\n最终回复长度:", ev(ws, "(function(){var b=document.querySelectorAll('.turn.bot .turn-body');"
                                   "return b.length? b[b.length-1].textContent.length : -1;})()"))
    print("最终按钮文字:", ev(ws, "document.getElementById('sendBtn').textContent"))
    print("meta 是否存在:", ev(ws, "!!document.querySelectorAll('.turn.bot .turn-meta').length"))
    print("对话轮数:", ev(ws, "document.querySelectorAll('.turn').length"))
    print("localStorage 历史条数:",
          ev(ws, "(function(){try{return JSON.parse(localStorage.getItem('p1_chat_history')||'[]').length}catch(e){return 'ERR'}})()"))

    # 截图
    try:
        shot = cmd(ws, "Page.captureScreenshot", format="png")
        data = shot.get("result", {}).get("data")
        if data:
            open(SHOT, "wb").write(base64.b64decode(data))
            print("\n截图:", SHOT)
    except Exception as e:  # noqa: BLE001
        print("截图失败:", e)

    print("\n--- JS 错误 ---")
    print("\n".join(errors) if errors else "✅ 无 JS 错误")
    print("\n--- console 前 12 条 ---")
    for l in logs[:12]:
        print("  ", l[:160])

    ws.close()


main()
