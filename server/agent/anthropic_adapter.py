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
    SYSTEM_PROMPT_TEMPLATE = """你是一个 AI 助手，正在通过 computer 工具操控一台 Mac 电脑。
你可以看到屏幕截图，并通过 computer 工具执行鼠标点击、键盘输入等操作来完成用户的任务。

截图尺寸: {image_width} x {image_height}
所有坐标都基于截图像素，范围 x:[0, {image_width}], y:[0, {image_height}]
截图的顶部和左侧有坐标刻度尺（每 100 像素标注一次），帮助你判断元素位置。

可用操作（通过 computer 工具的 action 参数）：
- screenshot: 获取当前屏幕截图
- click: 点击指定坐标，参数 coordinate=[x, y]
- double_click: 双击指定坐标，参数 coordinate=[x, y]
- right_click: 右键点击指定坐标，参数 coordinate=[x, y]
- type: 输入文本，参数 text="要输入的内容"
- key: 按键/快捷键，参数 text="ctrl+c" 或 "Return"
- scroll: 滚动，参数 coordinate=[x, y], direction="up"|"down"|"left"|"right", amount=3
- mouse_move: 移动鼠标，参数 coordinate=[x, y]
- drag: 拖拽，参数 start_coordinate=[x, y], end_coordinate=[x, y]
- wait: 等待，参数 duration=2.0

## 精确点击指南

坐标 coordinate=[x, y] 对应你看到的截图中的像素位置。定位目标元素时：
1. 找到目标 UI 元素在截图中的位置
2. 参考顶部 X 轴和左侧 Y 轴的刻度尺来确认坐标
3. 点击目标元素的中心而不是边缘

注意事项：
- 如果用户的消息中没有附带截图，说明该任务可能不需要操控屏幕。如果你确实需要看屏幕才能完成任务，请先使用 screenshot 动作获取截图
- 对于纯聊天、问答类问题（如"你好"、"解释一下xx"），直接文字回复即可，不需要截图或操控屏幕
- 每次操作后你会收到最新的屏幕截图，据此决定下一步操作
- 执行操作前先仔细观察屏幕内容和 UI 元素位置
- 点击坐标必须基于你看到的截图像素位置，对准目标元素的中心
- 如果操作没有预期效果，尝试其他方式（如使用快捷键）
- 任务完成后直接回复用户，不要再执行多余操作
- 如果遇到无法完成的情况，也请如实告知用户"""

    def __init__(self, executor, on_event, model_id: str | None = None):
        super().__init__(executor, on_event)
        self._api_config = None
        self._model = None  # 延迟初始化
        self._requested_model_id = model_id  # 前端指定的模型 ID
        # 截图坐标 → 物理坐标的缩放比例（在 _get_system_prompt 中计算）
        self._coord_scale_x = 1.0
        self._coord_scale_y = 1.0

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
                    "操控 Mac 电脑的工具。通过 action 参数指定操作类型，"
                    "不同操作需要不同的附加参数。每次调用后会返回最新的屏幕截图。"
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
            }
        ]

    def _get_system_prompt(self) -> str:
        """生成系统提示词（含截图实际尺寸信息）
        
        AI 看到的是带坐标刻度尺的画布（左侧 + 顶部各加 RULER_SIZE 像素），
        所以告诉 AI 的尺寸是画布尺寸（含刻度尺），与 AI 实际看到的图片一致。
        
        坐标映射链路：
        1. AI 返回画布像素坐标（含刻度尺偏移）
        2. _scale_coord 减去 RULER_SIZE 得到截图坐标
        3. 乘以缩放比例得到物理屏幕坐标
        """
        # 物理屏幕尺寸
        phys_w, phys_h = self.executor.screen.physical_screen_size
        
        # 模拟 take_screenshot 中 thumbnail 的实际行为
        # PIL thumbnail 保持比例缩小到 max_size 以内
        max_w, max_h = self.SCREENSHOT_MAX_SIZE
        scale = min(max_w / phys_w, max_h / phys_h, 1.0)
        # 用 round 而非 int，更接近 PIL thumbnail 的实际结果
        img_w = round(phys_w * scale)
        img_h = round(phys_h * scale)
        
        # 缓存缩放比例（截图坐标 → 物理坐标）
        self._coord_scale_x = phys_w / img_w
        self._coord_scale_y = phys_h / img_h
        
        # 画布尺寸 = 截图尺寸 + 刻度尺
        ruler = self.RULER_SIZE
        canvas_w = img_w + ruler
        canvas_h = img_h + ruler
        
        agent_log.info(
            f"[坐标映射] 物理屏幕 {phys_w}x{phys_h} → 截图 {img_w}x{img_h} "
            f"→ 画布 {canvas_w}x{canvas_h} (刻度尺={ruler}px), "
            f"缩放因子 scale={scale:.4f}, "
            f"坐标还原比 X={self._coord_scale_x:.4f} Y={self._coord_scale_y:.4f}"
        )
        
        return self.SYSTEM_PROMPT_TEMPLATE.format(
            image_width=canvas_w,
            image_height=canvas_h,
        )

    def _scale_coord(self, x: int, y: int) -> tuple[int, int]:
        """将 AI 返回的坐标映射回物理屏幕坐标
        
        AI 看到的是带坐标刻度尺的画布，它返回的坐标是画布像素坐标。
        需要先减去刻度尺偏移（RULER_SIZE），再映射到物理屏幕分辨率。
        """
        ruler = self.RULER_SIZE
        # 补偿刻度尺偏移：AI 返回的是画布坐标，需要减去刻度尺宽度得到截图坐标
        img_x = max(0, x - ruler)
        img_y = max(0, y - ruler)
        phys_x = int(round(img_x * self._coord_scale_x))
        phys_y = int(round(img_y * self._coord_scale_y))
        agent_log.debug(
            f"[坐标转换] 画布坐标 ({x}, {y}) → 截图坐标 ({img_x}, {img_y}) "
            f"→ 物理坐标 ({phys_x}, {phys_y}) "
            f"[刻度尺偏移={ruler}, 比例 X={self._coord_scale_x:.4f} Y={self._coord_scale_y:.4f}]"
        )
        return phys_x, phys_y

    def _parse_tool_action(self, tool_input: dict) -> Optional[ActionRequest]:
        """将 AI computer tool 的 input 解析为 ActionRequest
        
        AI 返回的坐标基于截图图片的像素坐标，
        这里通过 _scale_coord 映射回物理屏幕坐标后再执行。

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
            px, py = self._scale_coord(int(coord[0]), int(coord[1]))
            return ActionRequest(
                action=ActionType.CLICK,
                x=px, y=py,
                button="left",
            )

        elif action == "double_click":
            coord = tool_input.get("coordinate", [0, 0])
            px, py = self._scale_coord(int(coord[0]), int(coord[1]))
            return ActionRequest(
                action=ActionType.DOUBLE_CLICK,
                x=px, y=py,
            )

        elif action == "right_click":
            coord = tool_input.get("coordinate", [0, 0])
            px, py = self._scale_coord(int(coord[0]), int(coord[1]))
            return ActionRequest(
                action=ActionType.RIGHT_CLICK,
                x=px, y=py,
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
            px, py = self._scale_coord(int(coord[0]), int(coord[1]))
            direction = tool_input.get("direction", "down")
            amount = tool_input.get("amount", 3)
            return ActionRequest(
                action=ActionType.SCROLL,
                x=px, y=py,
                direction=direction,
                amount=amount,
            )

        elif action == "mouse_move":
            coord = tool_input.get("coordinate", [0, 0])
            px, py = self._scale_coord(int(coord[0]), int(coord[1]))
            return ActionRequest(
                action=ActionType.MOVE,
                x=px, y=py,
            )

        elif action == "drag":
            start = tool_input.get("start_coordinate", [0, 0])
            end = tool_input.get("end_coordinate", [0, 0])
            sx, sy = self._scale_coord(int(start[0]), int(start[1]))
            ex, ey = self._scale_coord(int(end[0]), int(end[1]))
            return ActionRequest(
                action=ActionType.DRAG,
                x=sx, y=sy,
                end_x=ex, end_y=ey,
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

        logger.debug(f"调用 API: {len(messages)} 条消息, model={model}")

        # 使用 asyncio.to_thread 包装同步 HTTP 调用
        _t0 = time.monotonic()
        response = await asyncio.to_thread(
            _call_messages_api,
            base_url=base_url,
            api_key=api_key,
            model=model,
            system=system_prompt,
            messages=messages,
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

    async def _run_loop(self, task: str, initial_screenshot: str) -> None:
        """Anthropic Computer Use 执行循环

        流程:
        1. 构造初始消息（用户任务 + 截图）
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
        
        # 根据是否有初始截图构造不同的消息
        if initial_screenshot:
            agent_log.info("[初始消息] 带截图，AI 可直接看到当前屏幕")
            user_content = [
                {
                    "type": "text",
                    "text": task,
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": initial_screenshot,
                    },
                },
            ]
        else:
            agent_log.info("[初始消息] 纯文本，AI 可通过 screenshot 动作主动获取屏幕截图")
            user_content = [
                {
                    "type": "text",
                    "text": task,
                },
            ]
        
        messages = [
            {
                "role": "user",
                "content": user_content,
            }
        ]

        while self.state == AgentState.RUNNING:
            # 检查暂停
            await self._check_pause()
            if self.state != AgentState.RUNNING:
                break

            # 调用 AI
            await self._emit(StepType.THINKING, {"content": "AI 正在分析屏幕..."})

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
                    tool_input = block.get("input", {})

                    agent_log.info(
                        f"[Tool Use] name={block.get('name', 'unknown')}, "
                        f"action={tool_input.get('action', 'unknown')}, "
                        f"id={tool_use_id}, "
                        f"params={json.dumps(tool_input, ensure_ascii=False)}"
                    )

                    # 解析动作
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

                    # 执行动作并截图
                    action_result, screenshot_b64 = await self._execute_and_screenshot(action_req)
                    agent_log.info(
                        f"[动作结果] action={action_req.action.value}, "
                        f"success={action_result.success}"
                        f"{', error=' + action_result.error if action_result.error else ''}"
                    )
                    tool_results.append(self._format_tool_result(
                        tool_use_id,
                        action_result,
                        screenshot_b64=screenshot_b64,
                    ))

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


