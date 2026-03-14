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


# ───────── 窗口捕获模式 ─────────

@router.get("/windows")
async def list_windows():
    """列出所有可见窗口（供前端选择窗口捕获模式）"""
    windows = executor.screen.list_windows()
    return {
        "windows": windows,
        "current_window_id": executor.screen._window_id,
        "pinned_ids": executor.screen.pinned_window_ids,
    }


@router.post("/window/select")
async def select_window(request: dict):
    """选择要捕获的窗口
    
    Body:
        {"window_id": 12345, "window_name": "...", "window_owner": "..."}
        或 {"window_id": null} 切回全屏模式
    """
    window_id = request.get("window_id")
    window_name = request.get("window_name", "")
    window_owner = request.get("window_owner", "")

    executor.screen.set_window(window_id, window_name, window_owner)

    return {
        "success": True,
        "mode": "window" if window_id is not None else "fullscreen",
        "screen_info": executor.screen.screen_info,
    }


@router.post("/window/pin")
async def pin_window(request: dict):
    """置顶窗口
    
    Body:
        {"window_id": 12345, "window_owner": "...", "window_name": "..."}
    """
    window_id = request.get("window_id")
    if window_id is None:
        return {"success": False, "error": "缺少 window_id"}
    
    owner = request.get("window_owner", "")
    name = request.get("window_name", "")
    result = executor.screen.pin_window(window_id, owner=owner, name=name)
    return {"success": result, "pinned_ids": executor.screen.pinned_window_ids}


@router.post("/window/unpin")
async def unpin_window(request: dict):
    """取消置顶窗口
    
    Body:
        {"window_id": 12345}
    """
    window_id = request.get("window_id")
    if window_id is None:
        return {"success": False, "error": "缺少 window_id"}
    
    result = executor.screen.unpin_window(window_id)
    return {"success": result, "pinned_ids": executor.screen.pinned_window_ids}


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
