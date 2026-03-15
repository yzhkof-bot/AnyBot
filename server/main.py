"""
AnyBot Mac 端服务 - 主入口
"""

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from loguru import logger

from .core.screen import ScreenCapture
from .core.input_control import InputController
from .core.action_executor import ActionExecutor
from .api import rest, websocket
from .stream import webrtc
from .agent import chat_api as agent_chat

# 日志配置
_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logger.remove()
# 终端输出（保留原有格式）
logger.add(sys.stderr, level="DEBUG", format="<green>{time:HH:mm:ss}</green> | <level>{level:7s}</level> | {message}")
# 文件日志 — 按天轮转，保留 7 天，方便排查问题
logger.add(
    str(_LOG_DIR / "anybot_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:7s} | {name}:{function}:{line} | {message}",
    rotation="00:00",      # 每天零点轮转
    retention="7 days",    # 保留 7 天
    encoding="utf-8",
    enqueue=True,          # 线程安全异步写入
)
# Agent 专用日志 — 单独文件，记录聊天和操控详情
logger.add(
    str(_LOG_DIR / "agent_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:7s} | {message}",
    rotation="00:00",
    retention="7 days",
    encoding="utf-8",
    enqueue=True,
    filter=lambda record: record["extra"].get("agent", False),
)

# FastAPI 应用
app = FastAPI(
    title="AnyBot",
    description="手机控制 Mac + AI Agent 操控平台",
    version="0.1.0",
)

# 初始化核心模块
screen = ScreenCapture(quality=75, max_size=(1920, 1200))
input_ctrl = InputController(screen.screen_info["width"], screen.screen_info["height"])
executor = ActionExecutor(screen, input_ctrl)

# 注入到 API 模块
rest.set_executor(executor)
websocket.set_executor(executor)
webrtc.set_executor(executor)
agent_chat.set_executor(executor)

# 注册路由
app.include_router(rest.router)
app.include_router(websocket.router)
app.include_router(webrtc.router)
app.include_router(agent_chat.router)

# 禁止浏览器缓存静态资源（开发阶段确保每次拿到最新文件）
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

app.add_middleware(NoCacheMiddleware)

# 静态文件 (手机端 Web 页面)
web_dir = Path(__file__).parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.get("/")
async def index():
    """手机端入口页面"""
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "AnyBot 服务运行中", "docs": "/docs"}


@app.get("/health")
async def health():
    return {"status": "ok", "screen": screen.screen_info}


@app.on_event("shutdown")
async def shutdown():
    await webrtc.close_all()
    screen.close()
    logger.info("AnyBot 服务已关闭")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
        log_level="info",
    )
