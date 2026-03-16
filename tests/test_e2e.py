#!/usr/bin/env python3
"""
AnyBot E2E 测试 - 使用 Playwright 自动化验证
"""

import sys
import json
import base64
import requests

BASE_URL = "http://localhost:8080"


def test_health():
    """测试健康检查接口"""
    print("测试 1: 健康检查")
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["screen"]["width"] > 0
    print(f"  ✅ 通过 - 屏幕: {data['screen']['width']}x{data['screen']['height']}")


def test_screenshot_jpeg():
    """测试 JPEG 截图"""
    print("测试 2: JPEG 截图 API")
    resp = requests.get(f"{BASE_URL}/api/screenshot", timeout=10)
    assert resp.status_code == 200
    assert "image/jpeg" in resp.headers.get("content-type", "")
    size_kb = len(resp.content) / 1024
    assert size_kb > 1
    print(f"  ✅ JPEG 截图: {size_kb:.1f} KB")


def test_screenshot_base64():
    """测试 base64 截图"""
    print("测试 3: Base64 截图 API")
    resp = requests.get(f"{BASE_URL}/api/screenshot/base64", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert "image_base64" in data
    decoded = base64.b64decode(data["image_base64"])
    assert len(decoded) > 1000
    print(f"  ✅ Base64 截图: {len(data['image_base64'])/1024:.1f} KB")


def test_screen_info():
    """测试屏幕信息"""
    print("测试 4: 屏幕信息 API")
    resp = requests.get(f"{BASE_URL}/api/screen/info", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert "width" in data and "height" in data
    print(f"  ✅ 屏幕: {data['width']}x{data['height']}")


def test_cursor():
    """测试光标位置"""
    print("测试 5: 光标位置 API")
    resp = requests.get(f"{BASE_URL}/api/cursor", timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert "x" in data and "y" in data
    print(f"  ✅ 光标: ({data['x']}, {data['y']})")


def test_action_screenshot():
    """测试 screenshot 动作"""
    print("测试 6: Action API - screenshot")
    resp = requests.post(f"{BASE_URL}/api/action", json={"action": "screenshot"}, timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "image_base64" in data["data"]
    print(f"  ✅ screenshot 动作成功")


def test_action_cursor():
    """测试 cursor_position 动作"""
    print("测试 7: Action API - cursor_position")
    resp = requests.post(f"{BASE_URL}/api/action", json={"action": "cursor_position"}, timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert "x" in data["data"]
    print(f"  ✅ 光标位置: {data['data']}")


def test_action_move():
    """测试 move 动作"""
    print("测试 8: Action API - move")
    resp = requests.post(f"{BASE_URL}/api/action", json={
        "action": "move", "x": 500, "y": 400
    }, timeout=5)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    print(f"  ✅ 移动鼠标到 (500, 400)")


def test_websocket_control():
    """测试 WebSocket 控制通道"""
    print("测试 9: WebSocket 控制通道")
    import websocket
    ws = websocket.create_connection("ws://localhost:9765/ws/control", timeout=5)
    try:
        ws.send(json.dumps({"action": "cursor_position"}))
        result = json.loads(ws.recv())
        assert result["success"] is True
        print(f"  ✅ WS 控制: 光标 {result['data']}")

        ws.send(json.dumps({"action": "move", "x": 600, "y": 500}))
        result = json.loads(ws.recv())
        assert result["success"] is True
        print(f"  ✅ WS 控制: 移动成功")
    finally:
        ws.close()


def test_websocket_screen():
    """测试 WebSocket 画面流"""
    print("测试 10: WebSocket 画面流")
    import websocket
    ws = websocket.create_connection("ws://localhost:8080/ws/screen", timeout=5)
    try:
        frames = 0
        for _ in range(5):
            data = ws.recv()
            assert isinstance(data, bytes)
            assert len(data) > 1000
            frames += 1
        print(f"  ✅ 收到 {frames} 帧画面, 首帧大小: {len(data)/1024:.1f} KB")
    finally:
        ws.close()


def test_web_page_playwright():
    """使用 Playwright 测试 Web 页面"""
    print("测试 11: Playwright Web 页面测试")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
        )
        page = context.new_page()

        # 加载页面
        page.goto(BASE_URL, timeout=10000)
        page.wait_for_load_state("domcontentloaded")
        print(f"  ✅ 页面加载完成")

        # 标题
        title = page.title()
        assert "AnyBot" in title
        print(f"  ✅ 标题: {title}")

        # 连接面板
        assert page.locator("#connect-btn").is_visible()
        print(f"  ✅ 连接按钮可见")

        # 点击连接
        page.locator("#connect-btn").click()
        page.wait_for_timeout(4000)

        # 检查连接面板隐藏
        overlay_hidden = page.locator("#connect-overlay").evaluate(
            "el => el.classList.contains('hidden')"
        )
        assert overlay_hidden, "连接面板应该隐藏"
        print(f"  ✅ 点击连接后面板隐藏")

        # 检查 canvas 有内容
        has_content = page.evaluate("""() => {
            const c = document.getElementById('screen-canvas');
            if (!c || c.width === 0) return false;
            const d = c.getContext('2d').getImageData(0, 0, 1, 1).data;
            return d[0] + d[1] + d[2] > 0;
        }""")
        assert has_content, "Canvas 应有画面"
        print(f"  ✅ Canvas 有画面内容")

        # 检查 FPS
        page.wait_for_timeout(1500)
        fps = page.locator("#fps-text").inner_text()
        print(f"  ✅ FPS: {fps}")

        # 工具栏
        page.locator("#btn-keyboard").click()
        page.wait_for_timeout(300)
        kb_show = page.locator("#keyboard-panel").evaluate("el => el.classList.contains('show')")
        assert kb_show
        print(f"  ✅ 键盘面板正常")

        # 截图保存
        page.screenshot(path="/Users/windye/Documents/wind/AnyBot/tests/screenshot.png")
        print(f"  ✅ 截图已保存到 tests/screenshot.png")

        browser.close()


def main():
    print()
    print("🤖 AnyBot E2E 自动化测试")
    print("=" * 50)

    tests = [
        test_health,
        test_screenshot_jpeg,
        test_screenshot_base64,
        test_screen_info,
        test_cursor,
        test_action_screenshot,
        test_action_cursor,
        test_action_move,
        test_websocket_control,
        test_websocket_screen,
        test_web_page_playwright,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  ❌ 失败: {e}")

    print()
    print("=" * 50)
    result = "全部通过 🎉" if failed == 0 else f"✅ {passed} 通过, ❌ {failed} 失败"
    print(f"结果: {result} (共 {passed + failed} 项)")
    print("=" * 50)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
