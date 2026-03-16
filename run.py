#!/usr/bin/env python3
"""AnyBot 启动脚本"""

import uvicorn


if __name__ == "__main__":
    print("🤖 AnyBot 启动中...")
    print("📱 局域网访问: http://<你的Mac IP>:8080")
    print("📖 API 文档: http://localhost:8080/docs")
    print()

    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
