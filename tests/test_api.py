#!/usr/bin/env python3
"""AnyBot API 快速测试"""
import json, base64, requests, websocket

BASE = "http://localhost:8080"
passed = failed = 0

def run(name, fn):
    global passed, failed
    try: fn(); passed += 1
    except Exception as e: failed += 1; print(f"  ❌ {name}: {e}")

print("\n🤖 AnyBot API 测试\n" + "=" * 50)

def t1():
    d = requests.get(f"{BASE}/health", timeout=5).json()
    assert d["status"] == "ok"
    print(f'T1 ✅ 健康检查: {d["screen"]["width"]}x{d["screen"]["height"]}')
run("health", t1)

def t2():
    r = requests.get(f"{BASE}/api/screenshot", timeout=10)
    assert r.status_code == 200 and "image/jpeg" in r.headers.get("content-type", "")
    print(f"T2 ✅ JPEG截图: {len(r.content)/1024:.1f}KB")
run("jpeg", t2)

def t3():
    d = requests.get(f"{BASE}/api/screenshot/base64", timeout=10).json()
    assert "image_base64" in d and len(base64.b64decode(d["image_base64"])) > 1000
    print(f'T3 ✅ Base64截图: {len(d["image_base64"])/1024:.1f}KB')
run("base64", t3)

def t4():
    d = requests.get(f"{BASE}/api/screen/info", timeout=5).json()
    assert "width" in d
    print(f"T4 ✅ 屏幕信息: {d['width']}x{d['height']}")
run("info", t4)

def t5():
    d = requests.get(f"{BASE}/api/cursor", timeout=5).json()
    assert "x" in d
    print(f"T5 ✅ 光标位置: ({d['x']},{d['y']})")
run("cursor", t5)

def t6():
    d = requests.post(f"{BASE}/api/action", json={"action": "screenshot"}, timeout=10).json()
    assert d["success"]
    print(f"T6 ✅ Action截图动作")
run("action_ss", t6)

def t7():
    d = requests.post(f"{BASE}/api/action", json={"action": "move", "x": 500, "y": 400}, timeout=5).json()
    assert d["success"]
    print(f"T7 ✅ Action移动鼠标")
run("action_move", t7)

def t8():
    ws = websocket.create_connection("ws://localhost:8080/ws/control", timeout=5)
    ws.send(json.dumps({"action": "cursor_position"}))
    r = json.loads(ws.recv())
    assert r["success"]
    ws.close()
    print(f"T8 ✅ WS控制通道: 光标{r['data']}")
run("ws_ctrl", t8)

def t9():
    ws = websocket.create_connection("ws://localhost:8080/ws/screen", timeout=5)
    sizes = []
    for _ in range(3):
        data = ws.recv()
        sizes.append(len(data))
    ws.close()
    print(f"T9 ✅ WS画面流: 3帧, 均{sum(sizes)/3/1024:.1f}KB/帧")
run("ws_screen", t9)

print(f"\n{'=' * 50}")
print(f"结果: ✅ {passed} 通过, ❌ {failed} 失败 (共{passed+failed}项)")
