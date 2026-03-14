#!/usr/bin/env python3
"""AnyBot 启动脚本"""

import uvicorn

if __name__ == "__main__":
    print("🤖 AnyBot 启动中...")
    print("📱 手机浏览器访问: http://<你的Mac IP>:9765")
    print("📖 API 文档: http://localhost:9765/docs")
    print()

    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=9765,
        reload=False,
        log_level="info",
    )
