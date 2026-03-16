"""
Anthropic Computer Use 协议适配器

将 Anthropic 的 Computer Use tool 格式映射到 AnyBot 的 ActionRequest/ActionResult。
实现 Agent 执行循环中的 AI 调用和 tool_use 解析逻辑。

**不依赖 anthropic SDK**，直接通过 HTTP 请求调用 Anthropic Messages API。
配置从项目根目录的 config.json 或环境变量读取。

Anthropic Computer Use 协议参考：
- Tool 类型: computer_20250124 (最新版)
- 操作: screenshot, click, double_click, right_click, type, key, scroll, mouse_move, drag
- 截图返回: base64 JPEG 作为 tool_result 中的 image content block
"""

import json
import os
import time
import asyncio
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from loguru import logger

from ..core.action_executor import ActionRequest, ActionResult, ActionType
from .base import AgentSession, AgentState, StepType

# Agent 专用日志器（会同时输出到 agent_*.log 和通用日志）
agent_log = logger.bind(agent=True)

# 项目根目录（server/agent/anthropic_adapter.py → 往上两级就是项目根目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# AnyBot 自己的配置文件路径
_CONFIG_PATH = _PROJECT_ROOT / "config.json"

# ───────── 可用模型注册表 ─────────
# 内部代理支持的模型列表
# id: 传给 API 的 model 名
# name: 前端显示名称
# provider: 模型厂商
# context: 上下文长度（None 表示默认）
# computer_use: 是否原生支持 Computer Use tool

AVAILABLE_MODELS = [
    {
        "id": "internal-model-opus-4-6-aws",
        "name": "Claude Opus 4.6",
        "provider": "Anthropic",
        "context": None,
        "computer_use": True,
        "default": True,
    },
    {
        "id": "internal-model-opus-aws",
        "name": "Claude Opus 4.5",
        "provider": "Anthropic",
        "context": None,
        "computer_use": True,
    },
    {
        "id": "internal-model-sonnet-4-6-aws",
        "name": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "context": None,
        "computer_use": True,
    },
    {
        "id": "internal-model-sonnet-aws",
        "name": "Claude Sonnet 4.5",
        "provider": "Anthropic",
        "context": None,
        "computer_use": True,
    },
    {
        "id": "internal-model",
        "name": "GPT-5.1",
        "provider": "OpenAI",
        "context": None,
        "computer_use": False,
    },
    {
        "id": "internal-model-codex",
        "name": "GPT-5.1-Codex",
        "provider": "OpenAI",
        "context": None,
        "computer_use": False,
    },
    {
        "id": "glm-5",
        "name": "GLM-5",
        "provider": "Zhipu",
        "context": 65536,
        "computer_use": False,
    },
    {
        "id": "kimi-k2.5",
        "name": "Kimi-K2.5",
        "provider": "Moonshot",
        "context": 65536,
        "computer_use": False,
    },
    {
        "id": "minimax-m2.5",
        "name": "Minimax-M2.5",
        "provider": "Minimax",
        "context": 131072,
        "computer_use": False,
    },
]

# 模型 ID → 模型信息快速查找
_MODEL_MAP = {m["id"]: m for m in AVAILABLE_MODELS}

# 默认模型 ID
DEFAULT_MODEL_ID = next(
    (m["id"] for m in AVAILABLE_MODELS if m.get("default")),
    AVAILABLE_MODELS[0]["id"]
)


def get_available_models() -> list[dict]:
    """获取可用模型列表（供 API 返回给前端）"""
    return AVAILABLE_MODELS


def get_model_info(model_id: str) -> dict | None:
    """根据模型 ID 获取模型信息"""
    return _MODEL_MAP.get(model_id)


def _get_api_config() -> dict:
    """获取 API 配置
    
    配置来源（优先级从高到低）：
    1. 环境变量（最高优先级）
    2. 项目根目录的 config.json（AnyBot 自己的配置文件）
    
    环境变量：
      ANYBOT_API_KEY    — API Key
      ANYBOT_BASE_URL   — 自定义 Base URL（代理/内部服务）
      ANYBOT_MODEL      — 自定义模型名
    
    config.json 格式：
    {
      "api_key": "your-api-key",
      "base_url": "https://api.anthropic.com",
      "model": "claude-sonnet-4-20250514"
    }
    
    Returns:
        dict: {"api_key": ..., "base_url": ..., "model": ...}
    """
    config = {
        "api_key": None,
        "base_url": None,
        "model": None,
    }
    
    # 1. 从项目自己的 config.json 读取
    if _CONFIG_PATH.is_file():
        try:
            with open(_CONFIG_PATH, "r") as f:
                file_cfg = json.load(f)
            config["api_key"] = file_cfg.get("api_key")
            config["base_url"] = file_cfg.get("base_url")
            config["model"] = file_cfg.get("model")
            logger.debug(f"从 {_CONFIG_PATH} 读取配置: base_url={config['base_url']}, model={config['model']}")
        except Exception as e:
            logger.warning(f"读取 {_CONFIG_PATH} 失败: {e}")
    else:
        logger.warning(f"配置文件不存在: {_CONFIG_PATH}，请复制 config.example.json 为 config.json 并填入 API Key")
    
    # 2. 环境变量覆盖（优先级更高）
    env_key = os.environ.get("ANYBOT_API_KEY")
    if env_key:
        config["api_key"] = env_key
    env_base_url = os.environ.get("ANYBOT_BASE_URL")
    if env_base_url:
        config["base_url"] = env_base_url
    env_model = os.environ.get("ANYBOT_MODEL")
    if env_model:
        config["model"] = env_model
    
    # 3. 校验
    if not config["api_key"]:
        raise ValueError(
            "未配置 API Key。\n"
            f"方式一：在项目根目录创建 config.json（参考 config.example.json）\n"
            "方式二：设置环境变量 export ANYBOT_API_KEY=your-key"
        )
    
    return config


# ───────── 纯 HTTP 调用 Anthropic Messages API ─────────

def _call_messages_api(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 4096,
) -> dict:
    """直接通过 HTTP 调用 Anthropic Messages API（同步，用 urllib）
    
    不依赖 anthropic SDK，自己组装请求。
    
    Returns:
        API 响应 JSON dict
    Raises:
        RuntimeError: API 调用失败
    """
    url = f"{base_url.rstrip('/')}/v1/messages"
    
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": tools,
        "messages": messages,
    }
    
    data = json.dumps(payload).encode("utf-8")
    
    # 记录请求详情（不记录完整 base64 图片数据，只记录元信息）
    msg_summary = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, str):
            msg_summary.append(f"{role}: {content[:100]}")
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    ctype = c.get("type", "?")
                    if ctype == "text":
                        parts.append(f"text({c.get('text', '')[:80]})")
                    elif ctype == "image":
                        parts.append("image(base64)")
                    elif ctype == "tool_result":
                        tid = c.get("tool_use_id", "?")[:12]
                        is_err = c.get("is_error", False)
                        parts.append(f"tool_result(id={tid}, err={is_err})")
                    elif ctype == "tool_use":
                        parts.append(f"tool_use({c.get('name', '?')})")
                    else:
                        parts.append(ctype)
            msg_summary.append(f"{role}: [{', '.join(parts)}]")
    
    agent_log.debug(
        f"[API 请求] model={model}, max_tokens={max_tokens}, "
        f"messages_count={len(messages)}, payload_size={len(data)} bytes\n"
        f"  消息概要:\n" + "\n".join(f"    {s}" for s in msg_summary)
    )
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_data = resp.read()
            result = json.loads(resp_data)
            # 记录响应概要
            usage = result.get("usage", {})
            content_blocks = result.get("content", [])
            block_types = [b.get("type", "?") for b in content_blocks]
            agent_log.debug(
                f"[API 响应] status=200, stop_reason={result.get('stop_reason')}, "
                f"blocks={block_types}, "
                f"tokens=in:{usage.get('input_tokens', '?')}/out:{usage.get('output_tokens', '?')}, "
                f"resp_size={len(resp_data)} bytes"
            )
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        agent_log.error(f"[API 错误] HTTP {e.code}: {body[:500]}")
        raise RuntimeError(
            f"API 请求失败 (HTTP {e.code}): {body}"
        )
    except urllib.error.URLError as e:
        agent_log.error(f"[API 错误] 连接失败: {e.reason}")
        raise RuntimeError(f"API 连接失败: {e.reason}")


class AnthropicComputerUseAdapter(AgentSession):
    """Anthropic Computer Use 适配器（纯 HTTP，不依赖 SDK）

    实现 AgentSession 的 _run_loop()，直接通过 HTTP 请求调用 Anthropic Messages API。
    使用 computer_20250124 tool 类型进行屏幕操控。
    
    配置来源（优先级从高到低）：
    1. 环境变量: ANYBOT_API_KEY, ANYBOT_BASE_URL, ANYBOT_MODEL
    2. 项目根目录 config.json
    """

    # 最大 token
    MAX_TOKENS = 4096

    # 系统提示词（动态拼接截图尺寸）
    SYSTEM_PROMPT_TEMPLATE = """你是一个 AI 助手，正在通过 computer 和 accessibility 工具操控一台 Mac 电脑。

屏幕尺寸: {image_width} x {image_height}
所有坐标都基于屏幕像素，范围 x:[0, {image_width}], y:[0, {image_height}]

## 可用工具

你有以下工具可用，每次循环可以根据任务需要自由选择最合适的工具，所有工具的使用权重是相同的。

### accessibility 工具（UI 控件树）
获取全屏 UI 控件树，覆盖屏幕上所有可交互区域，包含四个部分：
1. **前台应用**：当前前台应用的完整窗口和控件
2. **其他可见应用**：屏幕上其他可见窗口的应用（如微信、终端、Finder 等），按 z-order 排列
3. **系统菜单栏**：顶部菜单栏（应用菜单 + 右侧状态栏如 WiFi/电池/时钟）
4. **Dock 栏**：底部 Dock 上的所有应用图标

每个元素包含：角色（如 AXButton、AXTextField）、标题/描述、精确坐标 (x, y, width, height)
- 坐标与屏幕像素坐标完全一致
- 计算元素中心点：center_x = x + width/2, center_y = y + height/2
- 可以用来操作 Dock 上的应用图标、菜单栏项等系统 UI
- 适合场景：了解当前 UI 结构、获取元素精确坐标、读取文本内容

### computer 工具（屏幕操控）
通过 action 参数指定操作类型：
- **screenshot**: 获取当前屏幕截图，适合需要了解整体视觉布局、查看图片/颜色/样式等非结构化信息
- **click**: 点击指定坐标，参数 coordinate=[x, y]
- **double_click**: 双击指定坐标，参数 coordinate=[x, y]
- **right_click**: 右键点击指定坐标，参数 coordinate=[x, y]
- **type**: 输入文本，参数 text="要输入的内容"
- **key**: 按键/快捷键，参数 text="ctrl+c" 或 "Return"
- **scroll**: 滚动，参数 coordinate=[x, y], direction="up"|"down"|"left"|"right", amount=3
- **mouse_move**: 移动鼠标，参数 coordinate=[x, y]
- **drag**: 拖拽，参数 start_coordinate=[x, y], end_coordinate=[x, y]
- **wait**: 等待，参数 duration=2.0

## 工具选择指南

**每次循环你可以自由选择任意工具**，不必每次都先获取控件树或截图。根据当前任务情况灵活判断：

- **需要精确定位后操作**（click、double_click、right_click 等坐标点击操作）→ 优先用 accessibility 获取精确坐标，因为控件树的坐标比看截图估算更准确
- **需要了解整体页面布局、看图片/颜色/视觉效果** → 用 screenshot
- **输入文本** → 直接用 type，不需要先获取控件树或截图
- **按快捷键** → 直接用 key，不需要先获取控件树或截图
- **滚动页面** → 直接用 scroll
- **确认操作结果** → 根据需要选择 accessibility 或 screenshot，或者什么都不做直接继续下一步操作

关键原则：**选择最高效的方式完成任务**，不要做多余的操作。如果你已经知道要做什么（比如按快捷键、输入文本），直接操作即可，不需要先获取控件树或截图。

## 注意事项
- 对于纯聊天、问答类问题（如"你好"、"解释一下xx"），直接文字回复即可，不需要操控屏幕
- 点击坐标请瞄准目标元素的中心（从控件树坐标计算：x + width/2, y + height/2）
- 如果操作没有预期效果，尝试其他方式（如使用快捷键、截图看看什么情况）
- 任务完成后直接回复用户，不要再执行多余操作
- 如果遇到无法完成的情况，也请如实告知用户
- 并非所有应用都完整支持 Accessibility API，某些自定义控件可能不出现在控件树中，此时可用 screenshot 截图辅助判断"""

    def __init__(self, executor, on_event, model_id: str | None = None):
        super().__init__(executor, on_event)
        self._api_config = None
        self._model = None  # 延迟初始化
        self._requested_model_id = model_id  # 前端指定的模型 ID

    def _init_config(self):
        """初始化 API 配置（延迟加载）
        
        模型优先级：前端指定 > 环境变量/配置文件 > 默认模型
        """
        if self._api_config is None:
            self._api_config = _get_api_config()
            
            # 设置模型：前端指定 > 配置文件/环境变量 > 默认
            if self._requested_model_id:
                self._model = self._requested_model_id
            elif self._api_config["model"]:
                self._model = self._api_config["model"]
            else:
                self._model = DEFAULT_MODEL_ID
            
            base_url = self._api_config["base_url"] or "https://api.anthropic.com"
            model_info = get_model_info(self._model)
            model_display = model_info["name"] if model_info else self._model
            agent_log.info(
                f"[Agent 初始化] 模型: {self._model} ({model_display}), "
                f"API: {base_url}, "
                f"请求来源: {'前端指定' if self._requested_model_id else '配置文件/默认'}"
            )

    def _build_tools(self) -> list[dict]:
        """构建自定义 computer 操控 tool 定义

        使用标准 custom tool 类型（兼容所有 API 代理），
        通过 JSON Schema 描述操作参数。
        """
        return [
            {
                "name": "computer",
                "description": (
                    "操控 Mac 电脑的工具。通过 action 参数指定操作类型（如 screenshot、click、type、key、scroll 等），"
                    "不同操作需要不同的附加参数。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "screenshot", "click", "double_click", "right_click",
                                "type", "key", "scroll", "mouse_move", "drag", "wait"
                            ],
                            "description": "要执行的操作类型"
                        },
                        "coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "目标坐标 [x, y]，用于 click/double_click/right_click/scroll/mouse_move"
                        },
                        "text": {
                            "type": "string",
                            "description": "文本内容：type 操作的输入文本，或 key 操作的按键名（如 'ctrl+c', 'Return'）"
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                            "description": "滚动方向，仅 scroll 操作使用"
                        },
                        "amount": {
                            "type": "integer",
                            "description": "滚动量，仅 scroll 操作使用，默认 3"
                        },
                        "start_coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "拖拽起始坐标 [x, y]，仅 drag 操作使用"
                        },
                        "end_coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "拖拽结束坐标 [x, y]，仅 drag 操作使用"
                        },
                        "duration": {
                            "type": "number",
                            "description": "等待时长（秒），仅 wait 操作使用，默认 2.0"
                        },
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "accessibility",
                "description": (
                    "获取全屏 UI 控件树，覆盖屏幕上所有可交互区域。"
                    "包含四个部分：1) 前台应用的窗口和控件，2) 其他可见应用的窗口，3) 系统菜单栏（应用菜单 + 状态栏），4) Dock 栏上的应用图标。"
                    "返回每个 UI 元素的角色、标题、精确坐标 (x, y, width, height)。"
                    "坐标与截图像素坐标一致，可直接用于 click 操作的 coordinate 参数。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def _get_system_prompt(self) -> str:
        """生成系统提示词（含截图实际尺寸信息）"""
        phys_w, phys_h = self.executor.screen.physical_screen_size
        
        agent_log.info(
            f"[截图] 物理屏幕 {phys_w}x{phys_h}, "
            f"AI 坐标范围: x:[0,{phys_w}], y:[0,{phys_h}]"
        )
        
        return self.SYSTEM_PROMPT_TEMPLATE.format(
            image_width=phys_w,
            image_height=phys_h,
        )

    def _parse_tool_action(self, tool_input: dict) -> Optional[ActionRequest]:
        """将 AI computer tool 的 input 解析为 ActionRequest

        动作格式:
        - {"action": "screenshot"}
        - {"action": "click", "coordinate": [x, y]}
        - {"action": "double_click", "coordinate": [x, y]}
        - {"action": "right_click", "coordinate": [x, y]}
        - {"action": "type", "text": "..."}
        - {"action": "key", "text": "ctrl+c"}
        - {"action": "scroll", "coordinate": [x, y], "direction": "up"|"down"|"left"|"right", "amount": 3}
        - {"action": "mouse_move", "coordinate": [x, y]}
        - {"action": "drag", "start_coordinate": [x, y], "end_coordinate": [x, y]}
        """
        action = tool_input.get("action", "")
        
        # 记录 AI 返回的原始 tool_input
        agent_log.info(f"[解析动作] AI 原始输入: {json.dumps(tool_input, ensure_ascii=False)}")

        if action == "screenshot":
            return ActionRequest(action=ActionType.SCREENSHOT)

        elif action == "click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.CLICK,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
                button="left",
            )

        elif action == "double_click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.DOUBLE_CLICK,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
            )

        elif action == "right_click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.RIGHT_CLICK,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
            )

        elif action == "type":
            return ActionRequest(
                action=ActionType.TYPE,
                text=tool_input.get("text", ""),
            )

        elif action == "key":
            # Anthropic 格式: "ctrl+c", "Return", "space"
            # AnyBot 格式: ["ctrl", "c"], ["enter"], ["space"]
            key_str = tool_input.get("text", "")
            keys = self._parse_key_combo(key_str)
            return ActionRequest(
                action=ActionType.KEY,
                keys=keys,
            )

        elif action == "scroll":
            coord = tool_input.get("coordinate", [0, 0])
            direction = tool_input.get("direction", "down")
            amount = tool_input.get("amount", 3)
            return ActionRequest(
                action=ActionType.SCROLL,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
                direction=direction,
                amount=amount,
            )

        elif action == "mouse_move":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.MOVE,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
            )

        elif action == "drag":
            start = tool_input.get("start_coordinate", [0, 0])
            end = tool_input.get("end_coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.DRAG,
                x=max(0, int(start[0])), y=max(0, int(start[1])),
                end_x=max(0, int(end[0])), end_y=max(0, int(end[1])),
                duration=0.5,
            )

        elif action == "wait":
            return ActionRequest(
                action=ActionType.WAIT,
                duration=tool_input.get("duration", 2.0),
            )

        else:
            logger.warning(f"未知的动作: {action}")
            return None

    def _parse_key_combo(self, key_str: str) -> list[str]:
        """解析 Anthropic 的按键字符串为 AnyBot 的按键列表

        Anthropic 格式示例:
        - "Return" → ["enter"]
        - "ctrl+c" → ["ctrl", "c"]
        - "super+shift+s" → ["command", "shift", "s"]
        - "space" → ["space"]
        - "BackSpace" → ["backspace"]
        """
        # 按键名映射
        KEY_MAP = {
            "return": "enter",
            "enter": "enter",
            "backspace": "backspace",
            "delete": "delete",
            "space": "space",
            "tab": "tab",
            "escape": "escape",
            "esc": "escape",
            "up": "up",
            "down": "down",
            "left": "left",
            "right": "right",
            "home": "home",
            "end": "end",
            "pageup": "pageup",
            "pagedown": "pagedown",
            "super": "command",
            "super_l": "command",
            "meta": "command",
            "ctrl": "ctrl",
            "control": "ctrl",
            "alt": "option",
            "shift": "shift",
            "command": "command",
            "cmd": "command",
        }

        parts = key_str.split("+")
        keys = []
        for part in parts:
            part = part.strip()
            mapped = KEY_MAP.get(part.lower(), part.lower())
            keys.append(mapped)
        return keys

    def _format_tool_result(
        self,
        tool_use_id: str,
        result: ActionResult,
        screenshot_b64: str | None = None,
    ) -> dict:
        """将执行结果格式化为 Anthropic tool_result

        Args:
            tool_use_id: tool_use block 的 ID
            result: ActionExecutor 的执行结果
            screenshot_b64: 执行后的截图 base64（可选）

        Returns:
            Anthropic messages API 的 tool_result content block
        """
        content = []

        # 如果有截图，作为 image 类型返回
        if screenshot_b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": screenshot_b64,
                },
            })

        # 文本结果
        if result.error:
            content.append({
                "type": "text",
                "text": f"Error: {result.error}",
            })
        elif result.data:
            import json
            content.append({
                "type": "text",
                "text": json.dumps(result.data),
            })

        # 如果没有任何内容，给一个默认的
        if not content:
            content.append({
                "type": "text",
                "text": "Action executed successfully.",
            })

        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
            "is_error": not result.success,
        }

    @staticmethod
    def _strip_old_ui_trees(messages: list[dict], keep: int = 2) -> list[dict]:
        """裁剪消息历史中的旧控件树，只保留最近 N 次，避免 token 爆炸
        
        策略：
        - 遍历所有消息，找到 accessibility tool_result 中的控件树文本
        - 只保留最近 keep 次控件树，其余替换为文字占位符 "[控件树已省略]"
        
        Anthropic 格式中，控件树出现在 user 消息的 content list 中：
        - {"type": "tool_result", "tool_use_id": "...", "content": [{"type": "text", "text": "...控件树..."}]}
        
        识别方式：通过检查 assistant 消息中对应的 tool_use name 是否为 "accessibility"
        """
        import copy
        
        # 第一步：收集所有 accessibility tool_use 的 ID
        accessibility_tool_ids = set()
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "accessibility":
                        accessibility_tool_ids.add(block.get("id"))
        
        if not accessibility_tool_ids:
            return messages
        
        # 第二步：收集所有 accessibility tool_result 的位置 (msg_idx, content_idx)
        tree_positions = []
        for msg_idx, msg in enumerate(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for c_idx, block in enumerate(content):
                    if (isinstance(block, dict) 
                        and block.get("type") == "tool_result" 
                        and block.get("tool_use_id") in accessibility_tool_ids):
                        tree_positions.append((msg_idx, c_idx))
        
        if len(tree_positions) <= keep:
            return messages
        
        # 第三步：深拷贝并替换旧控件树
        stripped = copy.deepcopy(messages)
        old_positions = tree_positions[:-keep]  # 保留最后 keep 个
        
        removed_count = 0
        for msg_idx, c_idx in old_positions:
            block = stripped[msg_idx]["content"][c_idx]
            # 替换 content 中的控件树文本
            block["content"] = [{"type": "text", "text": "[控件树已省略]"}]
            removed_count += 1
        
        agent_log.info(
            f"[控件树裁剪] 消息历史中共 {len(tree_positions)} 次控件树，"
            f"移除了 {removed_count} 次旧控件树，保留最近 {keep} 次"
        )
        
        return stripped

    @staticmethod
    def _strip_old_images(messages: list[dict]) -> list[dict]:
        """裁剪消息历史中的图片，只保留最后一张，避免 token 爆炸
        
        策略：
        - 遍历所有消息，找到所有含图片的位置
        - 只保留最后一张图片，其余图片替换为文字占位符 "[截图已省略]"
        - 文字内容全部保留
        
        Anthropic 格式中，图片出现在 content list 中：
        - user 消息: {"type": "image", "source": {"type": "base64", ...}}
        - tool_result: content list 中的 {"type": "image", ...}
        """
        import copy
        
        # 第一遍：收集所有图片的位置 (msg_idx, content_idx)
        image_positions = []
        for msg_idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for c_idx, block in enumerate(content):
                    if isinstance(block, dict):
                        # user 消息中的 image block
                        if block.get("type") == "image":
                            image_positions.append((msg_idx, c_idx))
                        # tool_result 中嵌套的 image block
                        elif block.get("type") == "tool_result":
                            inner_content = block.get("content", [])
                            if isinstance(inner_content, list):
                                for ic_idx, inner_block in enumerate(inner_content):
                                    if isinstance(inner_block, dict) and inner_block.get("type") == "image":
                                        image_positions.append((msg_idx, c_idx, ic_idx))
        
        if len(image_positions) <= 1:
            # 只有 0 或 1 张图片，无需裁剪
            return messages
        
        # 第二遍：深拷贝并替换旧图片
        stripped = copy.deepcopy(messages)
        # 保留最后一张图片，替换其余的
        old_positions = image_positions[:-1]
        
        removed_count = 0
        for pos in old_positions:
            if len(pos) == 2:
                # 直接在 content list 中的 image
                msg_idx, c_idx = pos
                stripped[msg_idx]["content"][c_idx] = {
                    "type": "text",
                    "text": "[截图已省略]",
                }
                removed_count += 1
            elif len(pos) == 3:
                # tool_result 内部的 image
                msg_idx, c_idx, ic_idx = pos
                stripped[msg_idx]["content"][c_idx]["content"][ic_idx] = {
                    "type": "text",
                    "text": "[截图已省略]",
                }
                removed_count += 1
        
        agent_log.info(
            f"[图片裁剪] 消息历史中共 {len(image_positions)} 张图片，"
            f"移除了 {removed_count} 张旧图片，保留最后 1 张"
        )
        
        return stripped

    async def _call_model(self, messages: list[dict]) -> dict:
        """调用 Anthropic Messages API（纯 HTTP，不依赖 SDK）

        Args:
            messages: Anthropic 格式的消息历史

        Returns:
            API 响应 dict
        """
        self._init_config()
        tools = self._build_tools()
        system_prompt = self._get_system_prompt()
        model = self._model or DEFAULT_MODEL_ID
        base_url = self._api_config["base_url"] or "https://api.anthropic.com"
        api_key = self._api_config["api_key"]

        # 裁剪历史控件树，只保留最近 2 次，避免 token 爆炸
        trimmed_messages = self._strip_old_ui_trees(messages, keep=2)
        # 裁剪历史图片，只保留最后一张，避免 token 爆炸
        trimmed_messages = self._strip_old_images(trimmed_messages)

        logger.debug(f"调用 API: {len(trimmed_messages)} 条消息, model={model}")

        # 使用 asyncio.to_thread 包装同步 HTTP 调用
        _t0 = time.monotonic()
        response = await asyncio.to_thread(
            _call_messages_api,
            base_url=base_url,
            api_key=api_key,
            model=model,
            system=system_prompt,
            messages=trimmed_messages,
            tools=tools,
            max_tokens=self.MAX_TOKENS,
        )
        _elapsed = time.monotonic() - _t0

        usage = response.get("usage", {})
        agent_log.info(
            f"[模型调用] 耗时 {_elapsed:.2f}s, model={model}, "
            f"stop_reason={response.get('stop_reason')}, "
            f"content_blocks={len(response.get('content', []))}, "
            f"tokens: 输入={usage.get('input_tokens', '?')} 输出={usage.get('output_tokens', '?')}"
        )

        return response

    async def _run_loop(self, task: str) -> None:
        """Anthropic Computer Use 执行循环

        流程:
        1. 构造初始消息（纯任务文本，AI 自行决定是否获取控件树/截图）
        2. 调用 Anthropic API（纯 HTTP）
        3. 解析响应中的 tool_use blocks（dict 格式）
        4. 执行每个 tool_use 对应的操作，收集 tool_result
        5. 将 assistant 响应和 tool_result 追加到消息历史
        6. 如果 stop_reason == "tool_use"，回到步骤 2
        7. 如果 stop_reason == "end_turn"，任务完成
        """
        # 构造初始消息
        agent_log.info(f"{'='*60}")
        agent_log.info(f"[Agent 任务开始] 任务内容: {task}")
        agent_log.info(f"{'='*60}")
        
        # 纯任务文本，AI 根据系统提示词自行决定是否需要获取控件树或截图
        agent_log.info("[初始消息] 纯文本，AI 自行决定是否需要获取控件树或截图")
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": task,
                    },
                ],
            }
        ]

        while self.state == AgentState.RUNNING:
            # 检查暂停
            await self._check_pause()
            if self.state != AgentState.RUNNING:
                break

            # 调用 AI
            await self._emit(StepType.THINKING, {"content": "AI 正在思考..."})

            try:
                response = await self._call_model(messages)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"API 调用失败: {error_msg}")
                await self._emit(StepType.ERROR, {"content": f"AI 调用失败: {error_msg}"})
                break

            # 解析响应（纯 dict 格式）
            assistant_content = response.get("content", [])
            stop_reason = response.get("stop_reason", "end_turn")

            # 将 assistant 的响应追加到消息历史
            assistant_message = {
                "role": "assistant",
                "content": assistant_content,  # 已经是 dict list，直接用
            }
            messages.append(assistant_message)

            # 处理响应中的各个 content block
            tool_results = []
            has_tool_use = False

            for block in assistant_content:
                block_type = block.get("type", "")

                if block_type == "text":
                    # AI 的文字回复
                    text = block.get("text", "").strip()
                    if text:
                        agent_log.info(f"[AI 回复] {text}")
                        await self._emit(StepType.TEXT, {"content": text})

                elif block_type == "tool_use":
                    has_tool_use = True
                    tool_use_id = block.get("id", "")
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})

                    agent_log.info(
                        f"[Tool Use] name={tool_name}, "
                        f"action={tool_input.get('action', 'unknown')}, "
                        f"id={tool_use_id}, "
                        f"params={json.dumps(tool_input, ensure_ascii=False)}"
                    )

                    # accessibility 工具：获取 UI 控件树
                    if tool_name == "accessibility":
                        agent_log.info("[Tool Use] 获取 UI 控件树...")
                        await self._emit(StepType.ACTION, {
                            "content": "获取 UI 控件树...",
                            "action": "accessibility",
                        })
                        tree_text = await asyncio.to_thread(self.get_ui_tree)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": [{"type": "text", "text": tree_text}],
                            "is_error": False,
                        })
                        continue

                    # computer 工具：解析动作
                    action_req = self._parse_tool_action(tool_input)
                    if action_req is None:
                        # 无法解析的动作
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": [{"type": "text", "text": f"Unknown action: {tool_input.get('action')}"}],
                            "is_error": True,
                        })
                        continue

                    # 截图动作特殊处理（不需要执行操控，只需截图返回）
                    if action_req.action == ActionType.SCREENSHOT:
                        self._step_count += 1
                        await self._emit(StepType.SCREENSHOT, {"content": "AI 请求截图..."})
                        screenshot_b64 = await asyncio.to_thread(self.take_screenshot)
                        await self._emit(StepType.SCREENSHOT, {
                            "content": "截图完成",
                            "screenshot": screenshot_b64,
                        })
                        tool_results.append(self._format_tool_result(
                            tool_use_id,
                            ActionResult(action="screenshot", data={}),
                            screenshot_b64=screenshot_b64,
                        ))
                        continue

                    # 执行动作
                    action_result = await self._execute_action(action_req)
                    agent_log.info(
                        f"[动作结果] action={action_req.action.value}, "
                        f"success={action_result.success}"
                        f"{', error=' + action_result.error if action_result.error else ''}"
                    )
                    
                    # 构造 tool_result：只返回执行结果，AI 自行决定是否获取控件树或截图
                    result_content = []
                    if action_result.error:
                        result_content.append({
                            "type": "text",
                            "text": f"Error: {action_result.error}",
                        })
                    elif action_result.data:
                        result_content.append({
                            "type": "text",
                            "text": json.dumps(action_result.data),
                        })
                    if not result_content:
                        result_content.append({
                            "type": "text",
                            "text": "操作已执行成功。",
                        })
                    
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_content,
                        "is_error": not action_result.success,
                    })

            # 将 tool_results 追加到消息历史
            if tool_results:
                messages.append({
                    "role": "user",
                    "content": tool_results,
                })

            # 判断是否继续循环
            if stop_reason == "end_turn" or not has_tool_use:
                # AI 认为任务完成，或没有 tool_use
                agent_log.info(
                    f"{'='*60}\n"
                    f"[Agent 任务结束] stop_reason={stop_reason}, 总步骤={self._step_count}, "
                    f"消息历史={len(messages)} 条\n"
                    f"{'='*60}"
                )
                break

            # stop_reason == "tool_use"，继续循环
            agent_log.info(f"[循环继续] 第 {self._step_count} 步完成, stop_reason={stop_reason}, 继续下一轮...")

        # 保存消息历史
        self.messages = messages


