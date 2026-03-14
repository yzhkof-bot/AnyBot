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
        self._target_pid: int | None = None  # 键盘事件目标进程 PID
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

    def set_target_pid(self, pid: int | None):
        """设置键盘事件的目标进程 PID
        
        设置后，键盘事件通过 CGEventPostToPid 直接发送到目标进程，
        避免被 macOS 输入法拦截，也不受窗口焦点切换的影响。
        
        Args:
            pid: 目标进程 PID，None 表示清除（回退到全局广播模式）
        """
        if pid == self._target_pid:
            return  # PID 没变，不重复设置
        self._target_pid = pid
        if pid is not None:
            logger.info(f"键盘目标进程: PID={pid}")
        else:
            logger.info("键盘目标进程: 已清除（全局模式）")

    def _post_keyboard_event(self, event):
        """发送键盘事件 — 优先发送到目标进程
        
        如果设置了 target_pid，使用 CGEventPostToPid 直接发到目标进程，
        否则回退到 CGEventPost(kCGHIDEventTap) 全局广播。
        """
        if _HAS_QUARTZ and getattr(self, '_target_pid', None) is not None:
            Quartz.CGEventPostToPid(self._target_pid, event)
        else:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    def type_text(self, text: str):
        """输入文本
        
        策略：
        1. ASCII 文本 → 通过 CGEvent 逐字符发送键盘事件到目标进程
        2. 非 ASCII（中文等）→ 剪贴板 + Cmd+V 粘贴
        
        使用 CGEventPostToPid 直接发到目标进程，避免：
        - macOS 输入法拦截（中文输入法会吞掉英文字符进入预编辑）
        - 焦点抢夺（操作中途其他窗口弹出导致字符发错位置）
        """
        logger.debug(f"输入文本: '{text[:50]}...' target_pid={getattr(self, '_target_pid', None)}")

        try:
            if text.isascii():
                self._type_ascii_via_cgevent(text)
            else:
                # 非 ASCII（中文等），通过剪贴板粘贴
                import pyperclip
                pyperclip.copy(text)
                self.key(["command", "v"])
        except Exception as e:
            logger.error(f"输入文本失败: {e}，回退到 pyautogui")
            # fallback: 使用 pyautogui（全局广播）
            if text.isascii():
                pyautogui.typewrite(text, interval=0.02)
            else:
                import pyperclip
                pyperclip.copy(text)
                pyautogui.hotkey("command", "v")

    def _type_ascii_via_cgevent(self, text: str):
        """通过 CGEvent 逐字符发送 ASCII 文本到目标进程
        
        直接用 Quartz CGEvent 构造键盘事件，绕过 pyautogui 的输入法拦截问题。
        对需要 Shift 的字符（大写字母、特殊符号）自动添加 Shift 修饰。
        """
        import time
        from pyautogui._pyautogui_osx import keyboardMapping

        for char in text:
            # 查找 keyCode
            needs_shift = False
            if char in keyboardMapping:
                key_code = keyboardMapping[char]
            elif char.lower() in keyboardMapping:
                key_code = keyboardMapping[char.lower()]
                needs_shift = True
            elif pyautogui.isShiftCharacter(char):
                # 特殊符号需要 Shift（如 !, @, # 等）
                # pyautogui 内部用小写 key 映射
                key_code = keyboardMapping.get(char)
                if key_code is None:
                    logger.warning(f"无法映射字符: '{char}'，跳过")
                    continue
                needs_shift = True
            else:
                logger.warning(f"无法映射字符: '{char}'，跳过")
                continue

            if needs_shift:
                # Shift 按下
                shift_down = Quartz.CGEventCreateKeyboardEvent(None, keyboardMapping['shift'], True)
                self._post_keyboard_event(shift_down)
                time.sleep(0.005)

            # 按下
            key_down = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
            self._post_keyboard_event(key_down)
            time.sleep(0.005)

            # 释放
            key_up = Quartz.CGEventCreateKeyboardEvent(None, key_code, False)
            self._post_keyboard_event(key_up)

            if needs_shift:
                # Shift 释放
                shift_up = Quartz.CGEventCreateKeyboardEvent(None, keyboardMapping['shift'], False)
                self._post_keyboard_event(shift_up)

            time.sleep(0.015)  # 字符间隔

    def key(self, keys: list):
        """按键/组合键，如 ["command", "c"]
        
        优先使用 CGEventPostToPid 直接发到目标进程。
        """
        logger.debug(f"按键: {keys}")

        if not _HAS_QUARTZ or not keys:
            # 回退到 pyautogui
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            return

        try:
            self._key_via_cgevent(keys)
        except Exception as e:
            logger.error(f"CGEvent 按键失败: {e}，回退到 pyautogui")
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)

    def _key_via_cgevent(self, keys: list):
        """通过 CGEvent 发送按键/组合键
        
        按照修饰键在前、普通键在后的顺序：
        1. 按下所有修饰键
        2. 按下并释放普通键
        3. 逆序释放所有修饰键
        """
        import time
        from pyautogui._pyautogui_osx import keyboardMapping

        # 修饰键标志映射
        MODIFIER_FLAGS = {
            'command': Quartz.kCGEventFlagMaskCommand,
            'shift': Quartz.kCGEventFlagMaskShift,
            'control': Quartz.kCGEventFlagMaskControl,
            'option': Quartz.kCGEventFlagMaskAlternate,
            'alt': Quartz.kCGEventFlagMaskAlternate,
            'ctrl': Quartz.kCGEventFlagMaskControl,
            'cmd': Quartz.kCGEventFlagMaskCommand,
        }

        # 分离修饰键和普通键
        modifiers = []
        normal_keys = []
        modifier_flags = 0
        for k in keys:
            k_lower = k.lower()
            if k_lower in MODIFIER_FLAGS:
                modifiers.append(k_lower)
                modifier_flags |= MODIFIER_FLAGS[k_lower]
            else:
                normal_keys.append(k)

        # Step 1: 按下所有修饰键
        for mod in modifiers:
            key_code = keyboardMapping.get(mod)
            if key_code is None:
                logger.warning(f"无法映射修饰键: '{mod}'")
                continue
            event = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
            Quartz.CGEventSetFlags(event, modifier_flags)
            self._post_keyboard_event(event)
            time.sleep(0.005)

        # Step 2: 按下并释放普通键（带修饰标志）
        for k in normal_keys:
            key_code = keyboardMapping.get(k)
            if key_code is None:
                # 尝试小写
                key_code = keyboardMapping.get(k.lower())
            if key_code is None:
                logger.warning(f"无法映射键: '{k}'")
                continue

            down = Quartz.CGEventCreateKeyboardEvent(None, key_code, True)
            Quartz.CGEventSetFlags(down, modifier_flags)
            self._post_keyboard_event(down)
            time.sleep(0.005)

            up = Quartz.CGEventCreateKeyboardEvent(None, key_code, False)
            Quartz.CGEventSetFlags(up, modifier_flags)
            self._post_keyboard_event(up)
            time.sleep(0.005)

        # 如果没有普通键（纯修饰键如单独按 Shift），也要处理
        if not normal_keys and modifiers:
            # 单独按修饰键：已经按下了，后面会释放
            time.sleep(0.01)

        # Step 3: 逆序释放修饰键
        for mod in reversed(modifiers):
            key_code = keyboardMapping.get(mod)
            if key_code is None:
                continue
            event = Quartz.CGEventCreateKeyboardEvent(None, key_code, False)
            self._post_keyboard_event(event)
            time.sleep(0.005)

    def get_cursor_position(self) -> dict:
        """获取当前光标位置"""
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y}

    def _clamp(self, x: int, y: int):
        """限制坐标在屏幕范围内"""
        x = max(0, min(x, self.screen_width - 1))
        y = max(0, min(y, self.screen_height - 1))
        return x, y
