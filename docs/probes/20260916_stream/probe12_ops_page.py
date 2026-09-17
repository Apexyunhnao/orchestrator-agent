"""探针12：CDP 打开 /ops 观测台，确认链路/步骤渲染正常 + 截图。"""
import base64
import json
import os
import tempfile
import time
import urllib.request

import websocket

PORT = 9224
SHOT = os.path.join(tempfile.gettempdir(), "p1_ops_shot.png")
errors = []
_id = 0


def http_json(path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def collect(msg):
    if msg.get("method") == "Runtime.exceptionThrown":
        errors.append(str(msg["params"]["exceptionDetails"].get("text")))
    elif msg.get("method") == "Log.entryAdded":
        e = msg["params"]["entry"]
        if e["level"] == "error":
            errors.append(e["text"])


def cmd(ws, method, **params):
    global _id
    _id += 1
    my = _id
    ws.send(json.dumps({"id": my, "method": method, "params": params}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == my:
            return msg
        collect(msg)


def ev(ws, expr):
    r = cmd(ws, "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    res = r.get("result", {})
    if "exceptionDetails" in res:
        return f"<JS异常 {res['exceptionDetails'].get('text')}>"
    return res.get("result", {}).get("value")


def main():
    tab = http_json(f"/json/new?http://127.0.0.1:8010/ops", method="PUT")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    cmd(ws, "Page.enable")
    cmd(ws, "Runtime.enable")
    cmd(ws, "Log.enable")
    time.sleep(3)

    print("标题:", ev(ws, "document.title"))
    body = ev(ws, "document.body.innerText") or ""
    print("页面文本长度:", len(body))
    for kw in ("query_rag", "create_ticket", "supervisor", "查看执行详情"):
        print(f"  含 {kw!r}: {kw in body}")
    print("\n--- 页面文本片段 ---")
    print(body[:700].replace("\n\n", "\n"))

    shot = cmd(ws, "Page.captureScreenshot", format="png")
    d = shot.get("result", {}).get("data")
    if d:
        open(SHOT, "wb").write(base64.b64decode(d))
        print("\n截图:", SHOT)
    print("JS 错误:", errors if errors else "无")
    ws.close()


main()
