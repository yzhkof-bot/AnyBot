#!/usr/bin/env python3
"""
AnyBot MCP Server — 供外部工具（Claude Desktop、Cursor 等）调用

以 stdio 模式运行，通过 HTTP 请求本地 AnyBot REST API 获取数据。
仅暴露只读/查询类能力，不包含操控类接口。

Tools:
  - screenshot: 截取当前屏幕（返回 base64 JPEG）
  - list_windows: 获取所有可见窗口列表
  - screen_info: 获取屏幕分辨率等信息
  - cursor_position: 获取当前鼠标光标位置

使用方式:
  1. 确保 AnyBot 主服务正在运行（默认 http://localhost:8080）
  2. 运行: python mcp_server.py
  3. 或在 Claude Desktop 配置中添加此脚本

配置（环境变量）:
  ANYBOT_URL: AnyBot 服务地址（默认 http://localhost:8080）
"""

import os
import sys
import json
import urllib.request
import urllib.error
from typing import Any

from fastmcp import FastMCP

# AnyBot 主服务地址
ANYBOT_URL = os.environ.get("ANYBOT_URL", "http://localhost:8080")

# 创建 MCP Server
mcp = FastMCP(
    name="AnyBot",
    instructions=(
        "AnyBot 是一个 Mac 远程控制 + AI Agent 操控平台。"
        "通过此 MCP Server，你可以查看 Mac 的屏幕截图、窗口列表、"
        "屏幕信息和光标位置，帮助你理解用户当前的电脑使用状态。"
    ),
)


def _api_get(path: str) -> Any:
    """调用 AnyBot REST API (GET)

    Args:
        path: API 路径，如 "/api/screenshot/base64"

    Returns:
        JSON 解析后的数据

    Raises:
        ConnectionError: AnyBot 服务不可用
        RuntimeError: API 返回错误
    """
    url = f"{ANYBOT_URL}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"无法连接到 AnyBot 服务 ({ANYBOT_URL})。"
            f"请确保 AnyBot 主服务正在运行。错误: {e}"
        )
    except json.JSONDecodeError as e:
        raise RuntimeError(f"AnyBot API 返回了非 JSON 数据: {e}")


@mcp.tool()
def screenshot() -> str:
    """截取当前 Mac 全屏，返回 base64 编码的 JPEG 图片。

    始终截取完整桌面，不受窗口模式影响。

    适用场景：
    - 查看用户当前电脑屏幕内容
    - 分析用户正在使用的应用程序
    - 观察屏幕上的 UI 元素和布局

    Returns:
        base64 编码的 JPEG 截图字符串
    """
    data = _api_get("/api/screenshot/base64?mode=fullscreen")
    return data.get("image_base64", "")


@mcp.tool()
def list_windows() -> list[dict]:
    """获取 Mac 上所有可见窗口的列表。

    返回每个窗口的信息包括：
    - id: 窗口 ID
    - owner: 所属应用名称
    - name: 窗口标题
    - bounds: 位置和尺寸 {x, y, w, h}
    - offscreen: 是否有部分超出屏幕
    - pinned: 是否被置顶
    - order: Z-order 排序序号（0 = 最前面）

    适用场景：
    - 查看用户打开了哪些应用和窗口
    - 了解窗口布局和排列
    - 确认某个应用是否正在运行

    Returns:
        窗口信息列表，按 Z-order 排序（最前面的窗口排在前面）
    """
    data = _api_get("/api/windows")
    return data.get("windows", [])


@mcp.tool()
def screen_info() -> dict:
    """获取 Mac 屏幕信息。

    返回信息包括：
    - width: 屏幕宽度（像素）
    - height: 屏幕高度（像素）
    - window_mode: 是否处于窗口捕获模式
    - window_id/window_name/window_owner: 当前捕获的窗口信息（仅窗口模式下）

    适用场景：
    - 了解屏幕分辨率
    - 确认当前是全屏还是窗口捕获模式

    Returns:
        屏幕信息字典
    """
    return _api_get("/api/screen/info")


@mcp.tool()
def cursor_position() -> dict:
    """获取当前鼠标光标在屏幕上的位置。

    Returns:
        光标坐标 {"x": int, "y": int}
    """
    return _api_get("/api/cursor")


@mcp.tool()
def accessibility_snapshot() -> str:
    """获取当前 Mac 前台应用的 UI 控件树（Accessibility API）。

    返回所有可见 UI 元素的结构化信息，每个元素包含：
    - 角色（如 AXButton、AXTextField、AXStaticText）
    - 标题/描述文本
    - 精确坐标 (x, y, width, height)

    坐标与截图像素坐标完全一致。

    适用场景：
    - 需要精确定位 UI 元素的坐标时
    - 截图中元素较小、密集或难以辨识时
    - 需要获取 UI 元素的文本内容时
    - 需要了解应用的 UI 层次结构时

    注意：并非所有应用都完整支持 Accessibility API，
    某些自定义控件可能不出现在控件树中。

    Returns:
        缩进文本格式的 UI 控件树字符串
    """
    data = _api_get("/api/accessibility")
    return data.get("tree", "")


if __name__ == "__main__":
    # 默认 stdio 模式运行
    mcp.run()
