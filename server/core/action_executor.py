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
        """
        offset_x, offset_y = self.screen.get_window_offset()
        abs_x, abs_y = x + offset_x, y + offset_y
        if offset_x != 0 or offset_y != 0:
            logger.debug(f"坐标映射: ({x},{y}) + offset({offset_x},{offset_y}) → ({abs_x},{abs_y})")
        return (abs_x, abs_y)

    def _ensure_window_active(self):
        """确保目标窗口在前台（窗口模式下）
        
        macOS 的鼠标/键盘事件总是发送到屏幕最前面的窗口。
        如果用户选择了一个后台窗口，操作会发送到错误的窗口。
        在执行操控动作前调用此方法将目标窗口提升到前台。
        
        注意：activateWithOptions_ 是异步的，需要一小段延迟等待 macOS 完成
        窗口激活。否则紧接着的鼠标/键盘事件可能发送到旧的前台窗口。
        """
        if self.screen._window_id is not None:
            result = self.screen.activate_window()
            if result == "activated":
                # 刚执行了激活操作，等待 macOS 完成窗口 Z-order 切换
                import time
                time.sleep(0.08)  # 80ms 等待窗口前台切换完成

    def execute(self, req: ActionRequest) -> ActionResult:
        """执行一个操控动作"""
        try:
            logger.debug(f"执行动作: {req.action.value}")

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
