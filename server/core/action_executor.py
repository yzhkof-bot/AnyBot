"""
统一动作执行器 - 所有操控指令的入口
人的触摸操控和 AI Agent 指令都经过这里
"""

from typing import Any, Dict
from pydantic import BaseModel, Field
from enum import Enum
from loguru import logger

from .screen import ScreenCapture
from .input_control import InputController


class ActionType(str, Enum):
    SCREENSHOT = "screenshot"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MOVE = "move"
    DRAG = "drag"
    DRAG_START = "drag_start"
    DRAG_MOVE = "drag_move"
    DRAG_END = "drag_end"
    SCROLL = "scroll"
    TYPE = "type"
    KEY = "key"
    CURSOR_POSITION = "cursor_position"
    WAIT = "wait"


class ActionRequest(BaseModel):
    """操控动作请求"""
    action: ActionType
    x: int = 0
    y: int = 0
    end_x: int = 0
    end_y: int = 0
    button: str = "left"
    text: str = ""
    keys: list = Field(default_factory=list)
    direction: str = "down"
    amount: int = 3
    duration: float = 0.0


class ActionResult(BaseModel):
    """操控动作结果"""
    success: bool = True
    action: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class ActionExecutor:
    """统一动作执行器"""

    def __init__(self, screen: ScreenCapture, input_ctrl: InputController):
        self.screen = screen
        self.input_ctrl = input_ctrl

    def _map_coords(self, x: int, y: int) -> tuple:
        """将前端传来的坐标映射为屏幕绝对坐标
        
        窗口模式下，前端坐标是相对于窗口左上角的，
        需要加上窗口在屏幕上的偏移量。
        全屏模式下直接返回原坐标。
        
        额外处理：窗口可能部分超出屏幕（例如被用户拖到屏幕边缘外），
        此时计算出的绝对坐标可能为负数或超出屏幕分辨率。
        macOS 对屏幕外坐标的鼠标事件行为不可预测，
        因此将坐标裁剪（clamp）到屏幕可见范围 [0, screen_w/h) 内。
        """
        offset_x, offset_y = self.screen.get_window_offset()
        abs_x, abs_y = x + offset_x, y + offset_y
        
        # 获取物理屏幕尺寸用于裁剪（不是窗口截图尺寸）
        screen_w, screen_h = self.screen.physical_screen_size
        clamped = False
        if abs_x < 0:
            abs_x = 0
            clamped = True
        elif abs_x >= screen_w:
            abs_x = screen_w - 1
            clamped = True
        if abs_y < 0:
            abs_y = 0
            clamped = True
        elif abs_y >= screen_h:
            abs_y = screen_h - 1
            clamped = True
        
        if offset_x != 0 or offset_y != 0:
            msg = f"坐标映射: ({x},{y}) + offset({offset_x},{offset_y}) → ({abs_x},{abs_y})"
            if clamped:
                msg += " [已裁剪到屏幕范围]"
            logger.debug(msg)
        return (abs_x, abs_y)

    def _ensure_window_active(self):
        """确保目标窗口在前台（窗口模式下）
        
        macOS 的鼠标/键盘事件总是发送到屏幕最前面的窗口。
        如果用户选择了一个后台窗口，操作会发送到错误的窗口。
        在执行操控动作前调用此方法将目标窗口提升到前台。
        
        策略：
        1. 调用 activate_window() 激活目标窗口
           （内部已包含 AXRaise → AppleScript → open -a 的多级回退）
        2. 如果刚执行了激活操作，轮询验证窗口确实到达 Z-order 最前
        3. 如果被节流跳过，短暂等待即可（上次激活尚在生效）
        """
        if self.screen._window_id is not None:
            result = self.screen.activate_window()
            if result == "activated":
                # activate_window 内部已做验证和回退，这里再做一次最终确认
                import time
                for _ in range(4):  # 最多等 4 × 30ms = 120ms
                    time.sleep(0.03)
                    if self.screen._is_window_front():
                        break
                else:
                    logger.warning(f"窗口 {self.screen._window_id} 所有激活方式后仍未到达前台")
            elif result == "throttled":
                # 节流跳过了激活操作，短暂等待上次激活生效
                import time
                time.sleep(0.03)

    def _clamp_coords(self, x: int, y: int) -> tuple:
        """将绝对坐标裁剪到屏幕可见范围内（不做窗口偏移）
        
        Agent 传入的已经是物理屏幕绝对坐标，
        只需要做边界裁剪，不需要加窗口偏移。
        """
        screen_w, screen_h = self.screen.physical_screen_size
        clamped = False
        if x < 0:
            x = 0
            clamped = True
        elif x >= screen_w:
            x = screen_w - 1
            clamped = True
        if y < 0:
            y = 0
            clamped = True
        elif y >= screen_h:
            y = screen_h - 1
            clamped = True
        if clamped:
            logger.debug(f"坐标裁剪到屏幕范围: → ({x},{y})")
        return (x, y)

    def execute(self, req: ActionRequest) -> ActionResult:
        """执行一个操控动作"""
        try:
            logger.debug(f"执行动作: {req.action.value}")

            # 同步目标进程 PID 到 InputController（用于键盘事件直发）
            self.input_ctrl.set_target_pid(self.screen._window_pid)

            # 窗口模式下，执行操控动作前先确保目标窗口在前台
            # （排除截屏、光标查询、等待等不需要窗口聚焦的操作）
            _PASSIVE_ACTIONS = {ActionType.SCREENSHOT, ActionType.CURSOR_POSITION, ActionType.WAIT}
            if req.action not in _PASSIVE_ACTIONS:
                self._ensure_window_active()

            if req.action == ActionType.SCREENSHOT:
                b64 = self.screen.capture_base64()
                return ActionResult(action="screenshot", data={"image_base64": b64})

            elif req.action == ActionType.CLICK:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.click(ax, ay, button=req.button, click_type="single")
                return ActionResult(action="click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DOUBLE_CLICK:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.click(ax, ay, button="left", click_type="double")
                return ActionResult(action="double_click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.RIGHT_CLICK:
                ax, ay = self._map_coords(req.x, req.y)
                # 右键操作容易出问题（菜单可能显示在错误窗口），加详细日志
                if self.screen._window_id is not None:
                    bounds = self.screen._window_bounds
                    logger.info(f"右键点击: 窗口内({req.x},{req.y}) → 屏幕({ax},{ay}), "
                               f"窗口bounds={bounds}")
                self.input_ctrl.click(ax, ay, button="right", click_type="single")
                return ActionResult(action="right_click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.MOVE:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.move(ax, ay, duration=req.duration)
                return ActionResult(action="move", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG:
                ax, ay = self._map_coords(req.x, req.y)
                aex, aey = self._map_coords(req.end_x, req.end_y)
                self.input_ctrl.drag(ax, ay, aex, aey, duration=req.duration or 0.3)
                return ActionResult(action="drag", data={"x": req.x, "y": req.y, "end_x": req.end_x, "end_y": req.end_y})

            elif req.action == ActionType.DRAG_START:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.drag_start(ax, ay)
                return ActionResult(action="drag_start", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG_MOVE:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.drag_move(ax, ay)
                return ActionResult(action="drag_move", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG_END:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.drag_end(ax, ay)
                return ActionResult(action="drag_end", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.SCROLL:
                ax, ay = self._map_coords(req.x, req.y)
                self.input_ctrl.scroll(ax, ay, direction=req.direction, amount=req.amount)
                return ActionResult(action="scroll")

            elif req.action == ActionType.TYPE:
                self.input_ctrl.type_text(req.text)
                return ActionResult(action="type")

            elif req.action == ActionType.KEY:
                self.input_ctrl.key(req.keys)
                return ActionResult(action="key", data={"keys": req.keys})

            elif req.action == ActionType.CURSOR_POSITION:
                pos = self.input_ctrl.get_cursor_position()
                return ActionResult(action="cursor_position", data=pos)

            elif req.action == ActionType.WAIT:
                import asyncio
                import time
                time.sleep(req.duration or 1.0)
                return ActionResult(action="wait")

            else:
                return ActionResult(success=False, error=f"未知动作: {req.action}")

        except Exception as e:
            logger.error(f"动作执行失败: {e}")
            return ActionResult(success=False, action=req.action.value, error=str(e))

    def execute_absolute(self, req: ActionRequest) -> ActionResult:
        """执行操控动作 — 使用物理屏幕绝对坐标（Agent 专用）
        
        与 execute() 的区别：
        - 不经过 _map_coords（不加窗口偏移）
        - 只做屏幕边界裁剪
        - 不执行 _ensure_window_active（Agent 操控的是整个桌面）
        
        Agent 截取全屏截图，AI 返回的坐标就是物理屏幕绝对坐标，
        不需要再叠加窗口偏移。
        """
        try:
            logger.debug(f"执行动作(absolute): {req.action.value}")

            if req.action == ActionType.SCREENSHOT:
                b64 = self.screen.capture_base64()
                return ActionResult(action="screenshot", data={"image_base64": b64})

            elif req.action == ActionType.CLICK:
                ax, ay = self._clamp_coords(req.x, req.y)
                self.input_ctrl.click(ax, ay, button=req.button, click_type="single")
                return ActionResult(action="click", data={"x": ax, "y": ay})

            elif req.action == ActionType.DOUBLE_CLICK:
                ax, ay = self._clamp_coords(req.x, req.y)
                self.input_ctrl.click(ax, ay, button="left", click_type="double")
                return ActionResult(action="double_click", data={"x": ax, "y": ay})

            elif req.action == ActionType.RIGHT_CLICK:
                ax, ay = self._clamp_coords(req.x, req.y)
                self.input_ctrl.click(ax, ay, button="right", click_type="single")
                return ActionResult(action="right_click", data={"x": ax, "y": ay})

            elif req.action == ActionType.MOVE:
                ax, ay = self._clamp_coords(req.x, req.y)
                self.input_ctrl.move(ax, ay, duration=req.duration)
                return ActionResult(action="move", data={"x": ax, "y": ay})

            elif req.action == ActionType.DRAG:
                ax, ay = self._clamp_coords(req.x, req.y)
                aex, aey = self._clamp_coords(req.end_x, req.end_y)
                self.input_ctrl.drag(ax, ay, aex, aey, duration=req.duration or 0.3)
                return ActionResult(action="drag", data={"x": ax, "y": ay, "end_x": aex, "end_y": aey})

            elif req.action == ActionType.SCROLL:
                ax, ay = self._clamp_coords(req.x, req.y)
                self.input_ctrl.scroll(ax, ay, direction=req.direction, amount=req.amount)
                return ActionResult(action="scroll")

            elif req.action == ActionType.TYPE:
                self.input_ctrl.type_text(req.text)
                return ActionResult(action="type")

            elif req.action == ActionType.KEY:
                self.input_ctrl.key(req.keys)
                return ActionResult(action="key", data={"keys": req.keys})

            elif req.action == ActionType.CURSOR_POSITION:
                pos = self.input_ctrl.get_cursor_position()
                return ActionResult(action="cursor_position", data=pos)

            elif req.action == ActionType.WAIT:
                import time
                time.sleep(req.duration or 1.0)
                return ActionResult(action="wait")

            else:
                return ActionResult(success=False, error=f"未知动作: {req.action}")

        except Exception as e:
            logger.error(f"动作执行失败(absolute): {e}")
            return ActionResult(success=False, action=req.action.value, error=str(e))
