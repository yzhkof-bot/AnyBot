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

    def execute(self, req: ActionRequest) -> ActionResult:
        """执行一个操控动作"""
        try:
            logger.debug(f"执行动作: {req.action.value}")

            if req.action == ActionType.SCREENSHOT:
                b64 = self.screen.capture_base64()
                return ActionResult(action="screenshot", data={"image_base64": b64})

            elif req.action == ActionType.CLICK:
                self.input_ctrl.click(req.x, req.y, button=req.button, click_type="single")
                return ActionResult(action="click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DOUBLE_CLICK:
                self.input_ctrl.click(req.x, req.y, button="left", click_type="double")
                return ActionResult(action="double_click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.RIGHT_CLICK:
                self.input_ctrl.click(req.x, req.y, button="right", click_type="single")
                return ActionResult(action="right_click", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.MOVE:
                self.input_ctrl.move(req.x, req.y, duration=req.duration)
                return ActionResult(action="move", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG:
                self.input_ctrl.drag(req.x, req.y, req.end_x, req.end_y, duration=req.duration or 0.3)
                return ActionResult(action="drag", data={"x": req.x, "y": req.y, "end_x": req.end_x, "end_y": req.end_y})

            elif req.action == ActionType.DRAG_START:
                self.input_ctrl.drag_start(req.x, req.y)
                return ActionResult(action="drag_start", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG_MOVE:
                self.input_ctrl.drag_move(req.x, req.y)
                return ActionResult(action="drag_move", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.DRAG_END:
                self.input_ctrl.drag_end(req.x, req.y)
                return ActionResult(action="drag_end", data={"x": req.x, "y": req.y})

            elif req.action == ActionType.SCROLL:
                self.input_ctrl.scroll(req.x, req.y, direction=req.direction, amount=req.amount)
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
