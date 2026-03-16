"""
OpenAI 兼容模型适配器

为非 Anthropic 模型（GPT-5.1、GLM-5、Kimi、Minimax 等）提供 Computer Use 功能。
使用 OpenAI Chat Completions API 格式（/v1/chat/completions + function calling）。

与 anthropic_adapter.py 共享：
- 基类 AgentSession（截图、执行循环框架）
- 模型注册表（AVAILABLE_MODELS）
- API 配置（config.json / 环境变量）
- 系统提示词（_get_system_prompt）

差异点：
- API 端点: /v1/chat/completions（而非 /v1/messages）
- 消息格式: OpenAI Chat 格式（role/content/tool_calls/tool_call_id）
- 图片格式: image_url + data URI（而非 Anthropic 的 image source block）
- Tool 定义: OpenAI function calling（而非 Anthropic input_schema）
- 响应格式: choices[0].message（而非 content blocks）
"""

import json
import os
import time
import asyncio
import urllib.request
import urllib.error
from typing import Optional

from loguru import logger

from ..core.action_executor import ActionRequest, ActionType
from .base import AgentSession, AgentState, StepType
from .anthropic_adapter import (
    _get_api_config,
    DEFAULT_MODEL_ID,
    agent_log,
)

# ───────── OpenAI Chat Completions API 调用 ─────────

def _call_chat_completions_api(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 4096,
) -> dict:
    """通过 HTTP 调用 OpenAI Chat Completions API（同步，用 urllib）

    兼容 OpenAI、Azure OpenAI、以及各种 OpenAI 兼容代理。

    Returns:
        API 响应 JSON dict
    Raises:
        RuntimeError: API 调用失败
    """
    url = f"{base_url.rstrip('/')}/v1/chat/completions"

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }

    data = json.dumps(payload).encode("utf-8")

    # 记录请求概要
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
                    elif ctype == "image_url":
                        parts.append("image_url(base64)")
                    else:
                        parts.append(ctype)
            msg_summary.append(f"{role}: [{', '.join(parts)}]")
        # tool 消息
        tool_call_id = m.get("tool_call_id")
        if tool_call_id:
            msg_summary[-1] = f"{role}: tool_result(id={tool_call_id[:12]})"

    agent_log.debug(
        f"[OpenAI API 请求] model={model}, max_tokens={max_tokens}, "
        f"messages_count={len(messages)}, tools_count={len(tools)}, "
        f"payload_size={len(data)} bytes\n"
        f"  消息概要:\n" + "\n".join(f"    {s}" for s in msg_summary)
    )

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_data = resp.read()
            result = json.loads(resp_data)

            # 记录响应概要
            usage = result.get("usage", {})
            choices = result.get("choices", [])
            finish_reason = choices[0].get("finish_reason", "?") if choices else "no_choices"
            message = choices[0].get("message", {}) if choices else {}
            tool_calls = message.get("tool_calls", [])

            agent_log.debug(
                f"[OpenAI API 响应] status=200, finish_reason={finish_reason}, "
                f"tool_calls={len(tool_calls)}, "
                f"tokens=in:{usage.get('prompt_tokens', '?')}/out:{usage.get('completion_tokens', '?')}, "
                f"resp_size={len(resp_data)} bytes"
            )
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        agent_log.error(f"[OpenAI API 错误] HTTP {e.code}: {body[:500]}")
        raise RuntimeError(
            f"API 请求失败 (HTTP {e.code}): {body}"
        )
    except urllib.error.URLError as e:
        agent_log.error(f"[OpenAI API 错误] 连接失败: {e.reason}")
        raise RuntimeError(f"API 连接失败: {e.reason}")


class OpenAICompatAdapter(AgentSession):
    """OpenAI 兼容模型适配器

    使用 OpenAI Chat Completions API 格式实现 Computer Use 功能。
    支持所有 OpenAI 兼容的模型（GPT-5.1、GLM-5、Kimi、Minimax 等）。

    与 AnthropicComputerUseAdapter 的差异：
    - 使用 /v1/chat/completions 端点
    - 图片通过 image_url + data URI 发送
    - Tool 使用 OpenAI function calling 格式
    - 响应解析 choices[0].message 而非 content blocks
    """

    MAX_TOKENS = 4096

    # 系统提示词（与 Anthropic 适配器共用同一模板）
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

    def __init__(self, executor, on_event=None, model_id: str = None):
        super().__init__(executor, on_event)
        self._requested_model_id = model_id
        self._api_config = None
        self._model = None

    def _init_config(self):
        """初始化 API 配置（懒加载，首次调用时执行）"""
        if self._api_config is not None:
            return

        self._api_config = _get_api_config()

        # 模型优先级: 前端指定 > config.json > 默认
        if self._requested_model_id:
            self._model = self._requested_model_id
        elif self._api_config.get("model"):
            self._model = self._api_config["model"]
        else:
            self._model = DEFAULT_MODEL_ID

        agent_log.info(
            f"[OpenAI 适配器] 使用模型: {self._model}, "
            f"base_url: {self._api_config.get('base_url', 'N/A')}, "
            f"请求来源: {'前端指定' if self._requested_model_id else '配置文件/默认'}"
        )

    def _build_tools(self) -> list[dict]:
        """构建 OpenAI function calling 格式的 tool 定义"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "computer",
                    "description": (
                        "操控 Mac 电脑的工具。通过 action 参数指定操作类型（如 screenshot、click、type、key、scroll 等），"
                        "不同操作需要不同的附加参数。"
                    ),
                    "parameters": {
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
            },
            {
                "type": "function",
                "function": {
                    "name": "accessibility",
                    "description": (
                        "获取全屏 UI 控件树，覆盖屏幕上所有可交互区域。"
                        "包含四个部分：1) 前台应用的窗口和控件，2) 其他可见应用的窗口，3) 系统菜单栏（应用菜单 + 状态栏），4) Dock 栏上的应用图标。"
                        "返回每个 UI 元素的角色、标题、精确坐标 (x, y, width, height)。"
                        "坐标与截图像素坐标一致，可直接用于 click 操作的 coordinate 参数。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
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
        """将 tool call 参数解析为 ActionRequest（逻辑与 Anthropic 适配器一致）"""
        action = tool_input.get("action", "")
        agent_log.info(f"[解析动作] AI 原始输入: {json.dumps(tool_input, ensure_ascii=False)}")

        if action == "screenshot":
            return ActionRequest(action=ActionType.SCREENSHOT)

        elif action == "click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(action=ActionType.CLICK, x=max(0, int(coord[0])), y=max(0, int(coord[1])), button="left")

        elif action == "double_click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(action=ActionType.DOUBLE_CLICK, x=max(0, int(coord[0])), y=max(0, int(coord[1])))

        elif action == "right_click":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(action=ActionType.RIGHT_CLICK, x=max(0, int(coord[0])), y=max(0, int(coord[1])))

        elif action == "type":
            return ActionRequest(action=ActionType.TYPE, text=tool_input.get("text", ""))

        elif action == "key":
            key_str = tool_input.get("text", "")
            keys = self._parse_key_combo(key_str)
            return ActionRequest(action=ActionType.KEY, keys=keys)

        elif action == "scroll":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(
                action=ActionType.SCROLL,
                x=max(0, int(coord[0])), y=max(0, int(coord[1])),
                direction=tool_input.get("direction", "down"),
                amount=tool_input.get("amount", 3),
            )

        elif action == "mouse_move":
            coord = tool_input.get("coordinate", [0, 0])
            return ActionRequest(action=ActionType.MOVE, x=max(0, int(coord[0])), y=max(0, int(coord[1])))

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
            return ActionRequest(action=ActionType.WAIT, duration=tool_input.get("duration", 2.0))

        else:
            logger.warning(f"未知的动作: {action}")
            return None

    def _parse_key_combo(self, key_str: str) -> list[str]:
        """解析按键字符串为按键列表"""
        KEY_MAP = {
            "return": "enter", "enter": "enter",
            "backspace": "backspace", "delete": "delete",
            "space": "space", "tab": "tab",
            "escape": "escape", "esc": "escape",
            "up": "up", "down": "down", "left": "left", "right": "right",
            "home": "home", "end": "end",
            "pageup": "pageup", "pagedown": "pagedown",
            "super": "command", "super_l": "command", "meta": "command",
            "ctrl": "ctrl", "control": "ctrl",
            "alt": "option", "shift": "shift",
            "command": "command", "cmd": "command",
        }
        parts = key_str.split("+")
        return [KEY_MAP.get(p.strip().lower(), p.strip().lower()) for p in parts]

    @staticmethod
    def _make_image_content(screenshot_b64: str) -> dict:
        """构造 OpenAI 格式的图片内容块（data URI）"""
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{screenshot_b64}",
            },
        }

    @staticmethod
    def _strip_old_ui_trees(messages: list[dict], keep: int = 2) -> list[dict]:
        """裁剪消息历史中的旧控件树，只保留最近 N 次，避免 token 爆炸
        
        策略：
        - 遍历所有消息，找到 accessibility tool call 对应的 tool message
        - 只保留最近 keep 次控件树，其余替换为文字占位符 "[控件树已省略]"
        
        OpenAI 格式中，控件树出现在 role="tool" 的消息中：
        - {"role": "tool", "tool_call_id": "...", "content": "控件树文本..."}
        
        识别方式：通过检查 assistant 消息中对应的 tool_call function.name 是否为 "accessibility"
        """
        import copy
        
        # 第一步：收集所有 accessibility tool_call 的 ID
        accessibility_tc_ids = set()
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                if func.get("name") == "accessibility":
                    accessibility_tc_ids.add(tc.get("id"))
        
        if not accessibility_tc_ids:
            return messages
        
        # 第二步：收集所有 accessibility tool message 的位置 (msg_idx,)
        tree_positions = []
        for msg_idx, msg in enumerate(messages):
            if (msg.get("role") == "tool" 
                and msg.get("tool_call_id") in accessibility_tc_ids):
                tree_positions.append(msg_idx)
        
        if len(tree_positions) <= keep:
            return messages
        
        # 第三步：深拷贝并替换旧控件树
        stripped = copy.deepcopy(messages)
        old_positions = tree_positions[:-keep]  # 保留最后 keep 个
        
        removed_count = 0
        for msg_idx in old_positions:
            stripped[msg_idx]["content"] = "[控件树已省略]"
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
        
        OpenAI 格式中，图片出现在 user 消息的 content list 中：
        - {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
        """
        import copy
        
        # 收集所有图片的位置 (msg_idx, content_idx)
        image_positions = []
        for msg_idx, msg in enumerate(messages):
            content = msg.get("content")
            if isinstance(content, list):
                for c_idx, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        image_positions.append((msg_idx, c_idx))
        
        if len(image_positions) <= 1:
            return messages
        
        # 深拷贝并替换旧图片
        stripped = copy.deepcopy(messages)
        old_positions = image_positions[:-1]
        
        removed_count = 0
        for msg_idx, c_idx in old_positions:
            stripped[msg_idx]["content"][c_idx] = {
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
        """调用 OpenAI Chat Completions API"""
        self._init_config()
        tools = self._build_tools()
        model = self._model or DEFAULT_MODEL_ID
        base_url = self._api_config["base_url"] or "https://api.openai.com"
        api_key = self._api_config["api_key"]

        # 裁剪历史控件树，只保留最近 2 次，避免 token 爆炸
        trimmed_messages = self._strip_old_ui_trees(messages, keep=2)
        # 裁剪历史图片，只保留最后一张，避免 token 爆炸
        trimmed_messages = self._strip_old_images(trimmed_messages)

        logger.debug(f"[OpenAI] 调用 API: {len(trimmed_messages)} 条消息, model={model}")

        _t0 = time.monotonic()
        response = await asyncio.to_thread(
            _call_chat_completions_api,
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=trimmed_messages,
            tools=tools,
            max_tokens=self.MAX_TOKENS,
        )
        _elapsed = time.monotonic() - _t0

        usage = response.get("usage", {})
        choices = response.get("choices", [])
        finish_reason = choices[0].get("finish_reason", "?") if choices else "no_choices"
        agent_log.info(
            f"[模型调用] 耗时 {_elapsed:.2f}s, model={model}, "
            f"finish_reason={finish_reason}, "
            f"tokens: 输入={usage.get('prompt_tokens', '?')} 输出={usage.get('completion_tokens', '?')}"
        )

        return response

    async def _run_loop(self, task: str) -> None:
        """OpenAI 兼容模型的执行循环

        流程:
        1. 构造初始消息（system + user 任务，AI 自行决定是否获取控件树/截图）
        2. 调用 Chat Completions API
        3. 解析 tool_calls
        4. 执行动作，收集 tool 结果
        5. 将 assistant message 和 tool results 追加到消息历史
        6. 如果 finish_reason == "tool_calls"，回到步骤 2
        7. 如果 finish_reason == "stop"，任务完成
        """
        agent_log.info(f"{'='*60}")
        agent_log.info(f"[Agent 任务开始] (OpenAI 适配器) 任务内容: {task}")
        agent_log.info(f"{'='*60}")

        system_prompt = self._get_system_prompt()

        # 构造消息列表（纯任务文本，AI 自行决定是否需要获取控件树或截图）
        agent_log.info("[初始消息] 纯文本，AI 自行决定是否需要获取控件树或截图")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        while self.state == AgentState.RUNNING:
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

            # 解析响应
            choices = response.get("choices", [])
            if not choices:
                await self._emit(StepType.ERROR, {"content": "AI 返回了空响应"})
                break

            message = choices[0].get("message", {})
            finish_reason = choices[0].get("finish_reason", "stop")

            # 将 assistant 消息追加到历史
            # 需要保留完整的 message 结构（包含 tool_calls）
            assistant_msg = {"role": "assistant"}
            if message.get("content"):
                assistant_msg["content"] = message["content"]
            else:
                assistant_msg["content"] = None
            if message.get("tool_calls"):
                assistant_msg["tool_calls"] = message["tool_calls"]
            messages.append(assistant_msg)

            # 处理文字回复
            text_content = message.get("content", "")
            if text_content and text_content.strip():
                agent_log.info(f"[AI 回复] {text_content.strip()}")
                await self._emit(StepType.TEXT, {"content": text_content.strip()})

            # 处理 tool calls
            tool_calls = message.get("tool_calls", [])
            has_tool_calls = len(tool_calls) > 0

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                tc_function = tc.get("function", {})
                tc_name = tc_function.get("name", "")
                tc_args_str = tc_function.get("arguments", "{}")

                # 解析参数 JSON
                try:
                    tool_input = json.loads(tc_args_str)
                except json.JSONDecodeError:
                    agent_log.error(f"[Tool Call] 参数解析失败: {tc_args_str}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps({"error": f"Invalid JSON arguments: {tc_args_str}"}),
                    })
                    continue

                agent_log.info(
                    f"[Tool Call] name={tc_name}, "
                    f"action={tool_input.get('action', 'unknown')}, "
                    f"id={tc_id}, "
                    f"params={json.dumps(tool_input, ensure_ascii=False)}"
                )

                # accessibility 工具：获取 UI 控件树
                if tc_name == "accessibility":
                    agent_log.info("[Tool Call] 获取 UI 控件树...")
                    await self._emit(StepType.ACTION, {
                        "content": "获取 UI 控件树...",
                        "action": "accessibility",
                    })
                    tree_text = await asyncio.to_thread(self.get_ui_tree)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tree_text,
                    })
                    continue

                # computer 工具：解析动作
                action_req = self._parse_tool_action(tool_input)
                if action_req is None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps({"error": f"Unknown action: {tool_input.get('action')}"}),
                    })
                    continue

                # 截图动作特殊处理
                if action_req.action == ActionType.SCREENSHOT:
                    self._step_count += 1
                    await self._emit(StepType.SCREENSHOT, {"content": "AI 请求截图..."})
                    screenshot_b64 = await asyncio.to_thread(self.take_screenshot)
                    await self._emit(StepType.SCREENSHOT, {
                        "content": "截图完成",
                        "screenshot": screenshot_b64,
                    })
                    # OpenAI tool result: 图片作为文字描述 + 下一条 user 消息中附图
                    # 由于 tool message 不支持直接传图，用特殊方式处理
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps({"result": "截图已完成，请查看下方最新的屏幕截图。"}),
                    })
                    # 在 tool results 后追加一条带截图的 user 消息
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这是最新的屏幕截图："},
                            self._make_image_content(screenshot_b64),
                        ],
                    })
                    continue

                # 执行动作
                action_result = await self._execute_action(action_req)
                agent_log.info(
                    f"[动作结果] action={action_req.action.value}, "
                    f"success={action_result.success}"
                    f"{', error=' + action_result.error if action_result.error else ''}"
                )

                # 构造 tool result：只返回执行结果，AI 自行决定是否获取控件树或截图
                result_content = {}
                if action_result.error:
                    result_content["error"] = action_result.error
                elif action_result.data:
                    result_content["result"] = action_result.data
                else:
                    result_content["result"] = "操作执行成功。"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps(result_content, ensure_ascii=False),
                })

            # 判断是否继续
            if finish_reason == "stop" or not has_tool_calls:
                agent_log.info(
                    f"{'='*60}\n"
                    f"[Agent 任务结束] finish_reason={finish_reason}, 总步骤={self._step_count}, "
                    f"消息历史={len(messages)} 条\n"
                    f"{'='*60}"
                )
                break

            # 继续循环
            agent_log.info(
                f"[循环继续] 第 {self._step_count} 步完成, "
                f"finish_reason={finish_reason}, 继续下一轮..."
            )

        self.messages = messages
