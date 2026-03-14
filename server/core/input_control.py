"""
输入控制模块 - 使用 pyautogui 模拟鼠标/键盘操作
macOS 底层调用 Quartz Event (CGEvent)
"""

import pyautogui
from loguru import logger

# 关闭 pyautogui 的安全暂停（默认每次操作后暂停 0.1s）
pyautogui.PAUSE = 0.02
# 关闭 failsafe（鼠标移到左上角不会中断程序）
pyautogui.FAILSAFE = False

# macOS Quartz 直接调用（绕过 pyautogui PAUSE，用于高频 drag_move）
try:
    import Quartz
    _HAS_QUARTZ = True
except ImportError:
    _HAS_QUARTZ = False


class InputController:
    """输入控制器 - 统一的鼠标键盘操控接口"""

    def __init__(self, screen_width: int, screen_height: int):
        """
        Args:
            screen_width: 实际屏幕宽度
            screen_height: 实际屏幕高度
        """
        self.screen_width = screen_width
        self.screen_height = screen_height
        logger.info(f"输入控制器初始化: 屏幕 {screen_width}x{screen_height}")

    def click(self, x: int, y: int, button: str = "left", click_type: str = "single"):
        """鼠标点击"""
        x, y = self._clamp(x, y)
        logger.debug(f"点击: ({x}, {y}) button={button} type={click_type}")

        if click_type == "double":
            pyautogui.doubleClick(x, y, button=button)
        elif click_type == "triple":
            pyautogui.tripleClick(x, y, button=button)
        else:
            pyautogui.click(x, y, button=button)

    def move(self, x: int, y: int, duration: float = 0.0):
        """移动鼠标"""
        x, y = self._clamp(x, y)
        logger.debug(f"移动: ({x}, {y})")
        pyautogui.moveTo(x, y, duration=duration)

    def drag(self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.3):
        """拖拽（一次性完成，适合 API 调用）"""
        start_x, start_y = self._clamp(start_x, start_y)
        end_x, end_y = self._clamp(end_x, end_y)
        logger.debug(f"拖拽: ({start_x},{start_y}) → ({end_x},{end_y})")
        pyautogui.moveTo(start_x, start_y)
        pyautogui.drag(end_x - start_x, end_y - start_y, duration=duration)

    def drag_start(self, x: int, y: int, button: str = "left"):
        """流式拖拽 — 按下鼠标"""
        x, y = self._clamp(x, y)
        logger.debug(f"拖拽开始: ({x}, {y})")
        if _HAS_QUARTZ:
            # 直接用 Quartz 先移动再按下，绕过 PAUSE
            move_event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)
            down_event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down_event)
        else:
            pyautogui.moveTo(x, y, _pause=False)
            pyautogui.mouseDown(x=x, y=y, button=button, _pause=False)

    def drag_move(self, x: int, y: int):
        """流式拖拽 — 移动（不释放按键，高频调用零延迟）
        使用 Quartz CGEvent 直接发送鼠标移动事件，绕过 pyautogui 的全局 PAUSE
        """
        x, y = self._clamp(x, y)
        if _HAS_QUARTZ:
            # 直接通过 Quartz CGEvent 移动（左键按住状态的 mouseDragged）
            event = Quartz.CGEventCreateMouseEvent(
                None,
                Quartz.kCGEventLeftMouseDragged,
                (x, y),
                Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        else:
            pyautogui.moveTo(x, y, _pause=False)

    def drag_end(self, x: int, y: int, button: str = "left"):
        """流式拖拽 — 释放鼠标"""
        x, y = self._clamp(x, y)
        logger.debug(f"拖拽结束: ({x}, {y})")
        if _HAS_QUARTZ:
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        else:
            pyautogui.mouseUp(x=x, y=y, button=button, _pause=False)

    def scroll(self, x: int, y: int, direction: str = "down", amount: int = 3):
        """滚轮 — 使用像素级滚动事件，兼容所有 macOS 应用
        
        pyautogui.scroll() 使用 kCGScrollEventUnitLine（行级别），
        很多非编辑区域（网页、PDF、列表等）不响应行级滚动。
        改用 Quartz CGEvent 直接发送 kCGScrollEventUnitPixel 像素级滚动。
        """
        x, y = self._clamp(x, y)
        logger.debug(f"滚轮: ({x},{y}) direction={direction} amount={amount}")

        if _HAS_QUARTZ:
            # 先把鼠标移到目标位置（滚动事件作用于鼠标所在窗口）
            move_event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)

            # 像素级滚动量：每个 amount 单位 = 40 像素（接近真实触控板手感）
            PIXELS_PER_UNIT = 40

            if direction in ("up", "down"):
                pixel_delta = amount * PIXELS_PER_UNIT * (1 if direction == "up" else -1)
                # 创建垂直滚动事件（wheel1 = 垂直方向）
                scroll_event = Quartz.CGEventCreateScrollWheelEvent(
                    None,
                    Quartz.kCGScrollEventUnitPixel,
                    1,  # 1 个滚轮轴（垂直）
                    pixel_delta,
                )
            else:
                pixel_delta = amount * PIXELS_PER_UNIT * (-1 if direction == "left" else 1)
                # 创建水平滚动事件（wheel1=0, wheel2=水平方向）
                scroll_event = Quartz.CGEventCreateScrollWheelEvent(
                    None,
                    Quartz.kCGScrollEventUnitPixel,
                    2,  # 2 个滚轮轴（垂直+水平）
                    0,  # 垂直=0
                    pixel_delta,
                )

            Quartz.CGEventPost(Quartz.kCGHIDEventTap, scroll_event)
        else:
            # 没有 Quartz 时回退到 pyautogui
            clicks = amount if direction in ("up", "left") else -amount
            pyautogui.moveTo(x, y)
            if direction in ("up", "down"):
                pyautogui.scroll(clicks, x, y)
            else:
                pyautogui.hscroll(clicks, x, y)

    def type_text(self, text: str):
        """输入文本"""
        logger.debug(f"输入文本: {text[:50]}...")
        # pyautogui.typewrite 只支持 ASCII，中文用 pyperclip + hotkey
        try:
            # 尝试直接输入（ASCII 字符）
            if text.isascii():
                pyautogui.typewrite(text, interval=0.02)
            else:
                # 非 ASCII（如中文），通过剪贴板粘贴
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("command", "v")
        except Exception:
            # fallback: 逐字符输入
            for char in text:
                pyautogui.press(char) if len(char) == 1 and char.isascii() else None

    def key(self, keys: list):
        """按键/组合键，如 ["command", "c"]"""
        logger.debug(f"按键: {keys}")
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)

    def get_cursor_position(self) -> dict:
        """获取当前光标位置"""
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y}

    def _clamp(self, x: int, y: int):
        """限制坐标在屏幕范围内"""
        x = max(0, min(x, self.screen_width - 1))
        y = max(0, min(y, self.screen_height - 1))
        return x, y
