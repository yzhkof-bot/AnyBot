#!/usr/bin/env python3
"""AnyBot Playwright Web 页面测试"""
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
print("\n🤖 AnyBot Playwright 测试\n" + "=" * 50)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()

    # 1. 页面加载
    page.goto(BASE, timeout=10000)
    page.wait_for_load_state("domcontentloaded")
    print(f"T1 ✅ 页面加载: {page.title()}")

    # 2. 连接按钮可见
    assert page.locator("#connect-btn").is_visible()
    print("T2 ✅ 连接按钮可见")

    # 3. 点击连接
    page.locator("#connect-btn").click()
    page.wait_for_timeout(4000)
    hidden = page.locator("#connect-overlay").evaluate("el => el.classList.contains('hidden')")
    assert hidden, "连接面板未隐藏"
    print("T3 ✅ 连接成功, 面板隐藏")

    # 4. Canvas 有画面
    has_img = page.evaluate("""() => {
        const c = document.getElementById('screen-canvas');
        if (!c || c.width === 0) return false;
        const d = c.getContext('2d').getImageData(0, 0, 1, 1).data;
        return d[0]+d[1]+d[2] > 0;
    }""")
    assert has_img, "Canvas 无画面"
    print("T4 ✅ Canvas 有画面")

    # 5. FPS 显示
    page.wait_for_timeout(1500)
    fps = page.locator("#fps-text").inner_text()
    print(f"T5 ✅ FPS: {fps}")

    # 6. 点击 Canvas (模拟操控)
    box = page.locator("#screen-canvas").bounding_box()
    if box:
        page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
        page.wait_for_timeout(500)
        print("T6 ✅ Canvas 点击")

    # 7. 键盘面板
    page.locator("#btn-keyboard").click()
    page.wait_for_timeout(300)
    kb = page.locator("#keyboard-panel").evaluate("el => el.classList.contains('show')")
    assert kb, "键盘面板未显示"
    print("T7 ✅ 键盘面板切换")

    # 8. 截图保存
    page.screenshot(path="/Users/windye/Documents/wind/AnyBot/tests/screenshot.png")
    print("T8 ✅ 截图保存到 tests/screenshot.png")

    browser.close()

print(f"\n{'=' * 50}")
print("结果: 全部通过 🎉")
