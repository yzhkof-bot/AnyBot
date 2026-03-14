"""
AnyBot Mac 端服务 - 主入口
"""

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from loguru import logger

from .core.screen import ScreenCapture
from .core.input_control import InputController
from .core.action_executor import ActionExecutor
from .api import rest, websocket

# 日志配置
logger.remove()
logger.add(sys.stderr, level="DEBUG", format="<green>{time:HH:mm:ss}</green> | <level>{level:7s}</level> | {message}")

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

# 注册路由
app.include_router(rest.router)
app.include_router(websocket.router)

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
