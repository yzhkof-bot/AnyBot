"""
REST API 端点
"""

import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Response, UploadFile, File, Query
from loguru import logger

from ..core.action_executor import ActionRequest, ActionResult
from ..core.accessibility import get_accessibility_tree

router = APIRouter(prefix="/api", tags=["control"])

# 全局引用，在 main.py 中注入
executor = None


def set_executor(exec_instance):
    global executor
    executor = exec_instance


@router.get("/screenshot")
async def get_screenshot(mode: Optional[str] = Query(None, description="截图模式: fullscreen=强制全屏, 不传=跟随当前模式")):
    """获取当前屏幕截图
    
    Query Params:
        mode: fullscreen | (空)
            - fullscreen: 始终截全屏，不受窗口模式影响
            - 不传: 跟随当前模式（窗口模式截窗口，全屏模式截全屏）
    """
    if mode == "fullscreen":
        jpeg_bytes = executor.screen.capture_fullscreen_jpeg(quality=70)
    else:
        jpeg_bytes = executor.screen.capture_jpeg(quality=70)
    return Response(content=jpeg_bytes, media_type="image/jpeg")


@router.get("/screenshot/base64")
async def get_screenshot_base64(mode: Optional[str] = Query(None, description="截图模式: fullscreen=强制全屏, 不传=跟随当前模式")):
    """获取 base64 编码的屏幕截图
    
    Query Params:
        mode: fullscreen | (空)
            - fullscreen: 始终截全屏，不受窗口模式影响
            - 不传: 跟随当前模式
    """
    if mode == "fullscreen":
        b64 = executor.screen.capture_fullscreen_base64(quality=70)
    else:
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


# ───────── Accessibility 控件树 ─────────

@router.get("/accessibility")
async def get_accessibility():
    """获取当前前台应用的 UI 控件树（Accessibility API）
    
    返回缩进文本格式的控件树，包含每个 UI 元素的角色、标题、坐标和尺寸。
    坐标与截图像素坐标一致，可直接用于操控动作的坐标参数。
    """
    import asyncio
    tree_text = await asyncio.to_thread(get_accessibility_tree)
    return {"tree": tree_text}


@router.post("/screen/wake")
async def wake_screen():
    """唤醒屏幕（熄屏状态下通过 caffeinate 发送用户活动信号）"""
    try:
        subprocess.run(["caffeinate", "-u", "-t", "1"], timeout=3)
        logger.info("屏幕唤醒指令已发送")
        return {"success": True}
    except Exception as e:
        logger.error(f"唤醒屏幕失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/screen/brightness")
async def adjust_brightness(request: dict):
    """调节屏幕亮度（通过模拟 NX 媒体按键实现增减）

    Body:
        {"direction": "up"} 或 {"direction": "down"}
    """
    direction = request.get("direction", "up")
    # NX_KEYTYPE: 2 = brightness up, 3 = brightness down
    key_type = 2 if direction == "up" else 3
    try:
        _send_brightness_key(key_type)
        logger.info(f"屏幕亮度调节: {direction}")
        return {"success": True, "direction": direction}
    except Exception as e:
        logger.error(f"屏幕亮度调节失败: {e}")
        return {"success": False, "error": str(e)}


def _send_brightness_key(key_type: int):
    """通过 Quartz NX 媒体按键事件调节屏幕亮度"""
    import Quartz

    # NX_KEYDOWN = 0xa, NX_KEYUP = 0xb, NX_SUBTYPE_AUX_CONTROL_BUTTON = 8
    for flag, state in [(0xa00, 0xa), (0xb00, 0xb)]:
        ev = Quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flag, 0, 0, 0, 8,
            (key_type << 16) | (state << 8),
            -1,
        )
        Quartz.CGEventPost(0, ev.CGEvent())


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


# ───────── 文件上传 ─────────

# 默认上传目录：用户桌面
UPLOAD_DIR = Path.home() / "Desktop"


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件到 Mac（保存到桌面），图片自动复制到系统剪切板"""
    try:
        filename = file.filename or "unnamed"
        # 安全处理文件名：去除路径分隔符
        safe_name = Path(filename).name
        if not safe_name:
            safe_name = "unnamed"

        dest = UPLOAD_DIR / safe_name

        # 同名文件自动重命名：file.txt → file (1).txt
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = UPLOAD_DIR / f"{stem} ({counter}){suffix}"
                counter += 1

        content = await file.read()
        dest.write_bytes(content)

        logger.info(f"文件已上传: {dest} ({len(content)} bytes)")

        # 尝试将文件复制到系统剪切板
        copied = _copy_file_to_clipboard(dest)
        if copied:
            logger.info(f"文件已复制到剪切板: {dest}")

        return {
            "success": True,
            "filename": dest.name,
            "path": str(dest),
            "size": len(content),
            "copied_to_clipboard": copied,
        }
    except Exception as e:
        logger.error(f"文件上传失败: {e}")
        return {"success": False, "error": str(e)}


def _copy_file_to_clipboard(filepath: Path) -> bool:
    """将文件复制到 macOS 系统剪切板
    
    图片文件：以图片格式写入剪切板（可直接粘贴到聊天窗口、文档等）
    其他文件：以文件引用方式写入剪切板（可在 Finder 中粘贴）
    """
    try:
        suffix = filepath.suffix.lower()
        abs_path = str(filepath.resolve())
        # 转义路径中的特殊字符（双引号、反斜杠）
        escaped_path = abs_path.replace('\\', '\\\\').replace('"', '\\"')

        # 图片格式：以图片内容写入剪切板
        image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp'}
        if suffix in image_extensions:
            # 使用 osascript 调用 NSPasteboard 写入图片数据
            script = (
                'use framework "AppKit"\n'
                f'set img to (current application\'s NSImage\'s alloc()\'s initWithContentsOfFile:("{escaped_path}"))\n'
                'if img is not missing value then\n'
                '    set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
                '    pb\'s clearContents()\n'
                '    pb\'s writeObjects:{img}\n'
                '    return "ok"\n'
                'else\n'
                '    return "fail"\n'
                'end if'
            )
        else:
            # 非图片文件：以文件引用方式写入剪切板（Finder 可粘贴）
            script = (
                'use framework "AppKit"\n'
                'set pb to current application\'s NSPasteboard\'s generalPasteboard()\n'
                'pb\'s clearContents()\n'
                f'set fileURL to current application\'s NSURL\'s fileURLWithPath:"{escaped_path}"\n'
                'pb\'s writeObjects:{fileURL}\n'
                'return "ok"'
            )

        logger.debug(f"剪切板脚本执行: {filepath.name}")
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=10
        )
        logger.debug(f"osascript stdout: {result.stdout.strip()}, stderr: {result.stderr.strip()}, rc: {result.returncode}")

        if 'ok' in result.stdout:
            return True
        else:
            logger.warning(f"剪切板写入未成功: stdout={result.stdout.strip()}, stderr={result.stderr.strip()}")
            return False

    except Exception as e:
        logger.warning(f"复制到剪切板失败: {e}")
        return False
