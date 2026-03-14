"""
REST API 端点
"""

from fastapi import APIRouter, Response
from loguru import logger

from ..core.action_executor import ActionRequest, ActionResult

router = APIRouter(prefix="/api", tags=["control"])

# 全局引用，在 main.py 中注入
executor = None


def set_executor(exec_instance):
    global executor
    executor = exec_instance


@router.get("/screenshot")
async def get_screenshot():
    """获取当前屏幕截图"""
    jpeg_bytes = executor.screen.capture_jpeg(quality=70)
    return Response(content=jpeg_bytes, media_type="image/jpeg")


@router.get("/screenshot/base64")
async def get_screenshot_base64():
    """获取 base64 编码的屏幕截图（供 AI Agent 使用）"""
    b64 = executor.screen.capture_base64(quality=70)
    return {"image_base64": b64}


@router.get("/screen/info")
async def get_screen_info():
    """获取屏幕信息"""
    return executor.screen.screen_info


@router.get("/cursor")
async def get_cursor():
    """获取当前光标位置"""
    return executor.input_ctrl.get_cursor_position()


@router.post("/action", response_model=ActionResult)
async def execute_action(req: ActionRequest):
    """执行单个操控动作"""
    result = executor.execute(req)
    return result


@router.post("/actions")
async def execute_actions(actions: list[ActionRequest]):
    """批量执行操控动作"""
    results = []
    for req in actions:
        result = executor.execute(req)
        results.append(result)
        if not result.success:
            break
    return results
