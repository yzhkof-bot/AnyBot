# Agent 上下文机制

本文档详细说明 AnyBot Agent 在与 AI 模型交互时，上下文（消息历史、system prompt、tool 定义、图片、UI 控件树）是如何构建和携带的。

---

## 1. 整体架构

```
用户 → WebSocket → chat_api.py → 适配器(anthropic/openai) → AI API
                                       ↕
                                  base.py (截图/控件树/动作执行)
```

- **每次用户发送 `chat` 消息，都会创建一个全新的 `AgentSession`**（见 `chat_api.py` 第 224 行）
- **同一次任务内的多轮 loop 共享同一个 `messages` 列表**，AI 可以看到之前所有的对话和操作历史
- **不同任务之间不携带历史**：用户发送新的 `chat` 消息会创建新的 session，消息历史从零开始

### 跨任务上下文

```python
# chat_api.py 第 224 行
session = _create_session(
    executor=executor,
    on_event=on_event,
    model_id=msg.get("model") or ws_model_id,
)
```

**结论：当前没有跨任务的上下文延续。** 每次用户发新任务，都是一个全新的 session，全新的 messages 列表。上一次任务的对话历史不会带入下一次。

---

## 2. System Prompt 携带方式

System prompt 在每次调用 AI API 时都会传入，包含：
- 屏幕尺寸信息（动态填充）
- 可用工具说明
- 工具选择指南
- 注意事项

### Anthropic 适配器

System prompt 作为 API 请求中的 **`system` 参数**（与 `messages` 同级），**不在消息历史中**：

```python
# anthropic_adapter.py _call_messages_api()
payload = {
    "model": model,
    "max_tokens": max_tokens,
    "system": system_prompt,       # ← system prompt 独立传入
    "tools": tools,
    "messages": messages,          # ← 消息历史
}
```

**每次 API 调用都会重新传入完整的 system prompt**（不是只传一次）。

### OpenAI 适配器

System prompt 作为 `messages` 列表的**第一条消息**，在初始化时写入：

```python
# openai_adapter.py _run_loop() 第 524-527 行
messages = [
    {"role": "system", "content": system_prompt},   # ← system prompt 在消息历史中
    {"role": "user", "content": task},
]
```

**整个任务执行过程中 system prompt 一直在 messages 列表头部**，每次 API 调用都会带上。

### System Prompt 内容

动态生成，填入物理屏幕尺寸：

```python
SYSTEM_PROMPT_TEMPLATE = """你是一个 AI 助手，正在通过 computer 和 accessibility 工具操控一台 Mac 电脑。

屏幕尺寸: {image_width} x {image_height}
所有坐标都基于屏幕像素，范围 x:[0, {image_width}], y:[0, {image_height}]

## 可用工具
...（tool 使用说明）

## 工具选择指南
...（选择策略）

## 注意事项
...
"""
```

---

## 3. Tool 定义携带方式

Tool 定义每次 API 调用都会传入（与 messages 同级），AI 通过 tool 定义知道有哪些工具可用。

### Anthropic 格式

```python
tools = [
    {
        "name": "computer",
        "description": "操控 Mac 电脑的工具...",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["screenshot", "click", ...]},
                "coordinate": {"type": "array", ...},
                "text": {"type": "string", ...},
                ...
            },
            "required": ["action"],
        },
    },
    {
        "name": "accessibility",
        "description": "获取全屏 UI 控件树...",
        "input_schema": {"type": "object", "properties": {}},
    },
]
```

### OpenAI 格式

```python
tools = [
    {
        "type": "function",
        "function": {
            "name": "computer",
            "description": "操控 Mac 电脑的工具...",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [...]},
                    ...
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accessibility",
            "description": "获取全屏 UI 控件树...",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
```

---

## 4. 消息历史（Messages）的完整结构

### 4.1 Anthropic 适配器的消息流

```
API 调用时传入:
  system: "你是一个 AI 助手..."          ← 每次都传
  tools: [computer, accessibility]       ← 每次都传
  messages: [                            ← 累积的消息历史
    {role: "user", content: [用户任务文本]},

    {role: "assistant", content: [       ← AI 第 1 轮响应
      {type: "text", text: "..."},       ← AI 的思考/回复文字
      {type: "tool_use", id: "xxx", name: "accessibility", input: {}},
    ]},

    {role: "user", content: [            ← 第 1 轮 tool_result
      {type: "tool_result", tool_use_id: "xxx", content: [{type: "text", text: "控件树文本..."}]},
    ]},

    {role: "assistant", content: [       ← AI 第 2 轮响应
      {type: "tool_use", id: "yyy", name: "computer", input: {action: "click", coordinate: [100, 200]}},
    ]},

    {role: "user", content: [            ← 第 2 轮 tool_result
      {type: "tool_result", tool_use_id: "yyy", content: [{type: "text", text: "操作已执行成功。"}]},
    ]},

    {role: "assistant", content: [       ← AI 第 3 轮响应
      {type: "tool_use", id: "zzz", name: "computer", input: {action: "screenshot"}},
    ]},

    {role: "user", content: [            ← 截图作为 tool_result 返回
      {type: "tool_result", tool_use_id: "zzz", content: [
        {type: "image", source: {type: "base64", media_type: "image/jpeg", data: "base64..."}},
      ]},
    ]},

    ...（继续循环，所有历史都累积在这个列表中）
  ]
```

### 4.2 OpenAI 适配器的消息流

```
messages: [
  {role: "system", content: "你是一个 AI 助手..."},   ← system prompt 在第一条

  {role: "user", content: "打开浏览器"},               ← 用户任务（纯文本）

  {role: "assistant", content: null, tool_calls: [     ← AI 第 1 轮：调用 accessibility
    {id: "tc1", function: {name: "accessibility", arguments: "{}"}},
  ]},

  {role: "tool", tool_call_id: "tc1",                 ← 控件树作为 tool message 返回
   content: "=== 前台应用 ===\n[AXWindow]..."},

  {role: "assistant", content: null, tool_calls: [     ← AI 第 2 轮：点击
    {id: "tc2", function: {name: "computer", arguments: "{\"action\":\"click\",\"coordinate\":[100,200]}"}},
  ]},

  {role: "tool", tool_call_id: "tc2",                 ← 操作结果
   content: "{\"result\":\"操作执行成功。\"}"},

  {role: "assistant", content: null, tool_calls: [     ← AI 第 3 轮：截图
    {id: "tc3", function: {name: "computer", arguments: "{\"action\":\"screenshot\"}"}},
  ]},

  {role: "tool", tool_call_id: "tc3",                 ← 截图 tool result (文字)
   content: "{\"result\":\"截图已完成，请查看下方最新的屏幕截图。\"}"},

  {role: "user", content: [                            ← 截图作为额外的 user 消息（因 tool message 不支持图片）
    {type: "text", text: "这是最新的屏幕截图："},
    {type: "image_url", image_url: {url: "data:image/jpeg;base64,..."}},
  ]},

  ...（继续循环）
]
```

---

## 5. 截图（Screenshot）的携带方式

### 5.1 何时产生截图

截图**不是自动附带的**。只有 AI 主动调用 `computer` tool 的 `screenshot` action 时才会截图。

```
AI → tool_use: {name: "computer", input: {action: "screenshot"}}
框架 → 调用 take_screenshot() → 返回 base64 JPEG
框架 → 将截图放入 tool_result 返回给 AI
```

### 5.2 截图在消息中的格式

#### Anthropic 格式

截图作为 `tool_result` 中的 `image` block：

```python
{
    "type": "tool_result",
    "tool_use_id": "xxx",
    "content": [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "<base64 编码的 JPEG 图片>"
            }
        }
    ],
    "is_error": False,
}
```

#### OpenAI 格式

由于 OpenAI 的 `tool` role 消息不支持直接传图片，截图分两步：

1. **tool message**：返回文字提示
   ```python
   {"role": "tool", "tool_call_id": "xxx", "content": "{\"result\":\"截图已完成，请查看下方最新的屏幕截图。\"}"}
   ```

2. **追加一条 user 消息**：携带图片
   ```python
   {
       "role": "user",
       "content": [
           {"type": "text", "text": "这是最新的屏幕截图："},
           {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<base64数据>"}},
       ]
   }
   ```

### 5.3 截图参数

```python
# base.py
SCREENSHOT_QUALITY = 70         # JPEG 质量
img = executor.screen.capture_fullscreen_raw()  # 全屏物理分辨率截图
```

- 始终全屏截图，不受前端窗口模式影响
- JPEG quality=70，optimize=True
- 不做任何标注/叠加

### 5.4 历史截图裁剪策略

**两个适配器都实现了 `_strip_old_images()` 方法**，在每次调用 API 前裁剪消息历史中的旧截图：

- **只保留最后 1 张截图**
- 其余截图替换为 `[截图已省略]` 占位文本
- 文字内容全部保留
- 目的：**避免 token 爆炸**

```python
# 调用链
_call_model(messages)
  → trimmed_messages = self._strip_old_images(messages)  # 先裁剪
  → 调用 API（用裁剪后的 messages）

# 注意：裁剪是对 deep copy 操作，不修改原始 messages 列表
```

---

## 6. UI 控件树（Accessibility / UITree）的携带方式

### 6.1 何时产生控件树

控件树**不是自动附带的**。只有 AI 主动调用 `accessibility` tool 时才会获取。

```
AI → tool_use: {name: "accessibility", input: {}}
框架 → 调用 get_ui_tree() → 调用 AccessibilityInspector.get_tree()
框架 → 返回纯文本格式的控件树
```

### 6.2 控件树在消息中的格式

#### Anthropic 格式

作为 `tool_result` 中的 `text` block：

```python
{
    "type": "tool_result",
    "tool_use_id": "xxx",
    "content": [
        {
            "type": "text",
            "text": "=== 前台应用: Code (pid=1234) ===\n[AXWindow] \"main.py\" (0, 38, 1920, 1080)\n  [AXGroup] (0, 38, 1920, 50)\n    [AXButton] \"关闭\" (7, 42, 14, 14)\n    ..."
        }
    ],
    "is_error": False,
}
```

#### OpenAI 格式

作为 `tool` role 消息的 `content`（纯文本）：

```python
{
    "role": "tool",
    "tool_call_id": "xxx",
    "content": "=== 前台应用: Code (pid=1234) ===\n[AXWindow] \"main.py\" (0, 38, 1920, 1080)\n  ..."
}
```

### 6.3 控件树数据大小

根据实际测量（2026-03-16 的 5 次采集）：

| 指标 | 数值 |
|------|------|
| 行数 | ~680 行 |
| 字符数 | ~32K 字符 |
| 文件大小 | ~34 KB |
| 获取耗时 | 0.25~0.39 秒 |
| 估算 token | ~8K~10K tokens |

### 6.4 控件树内容结构

控件树分为 4 个 section：

```
=== 前台应用: AppName (pid=1234) ===
[AXWindow] "窗口标题" (x, y, w, h)
  [AXGroup] (x, y, w, h)
    [AXButton] "按钮名" (x, y, w, h)
    [AXTextField] "输入框" (x, y, w, h)
    ...

=== 其他可见应用 ===
--- AppName2 (pid=5678) ---
[AXWindow] "窗口标题" (x, y, w, h)
  ...

=== 系统菜单栏 ===
[AXMenuBar] (x, y, w, h)
  [AXMenuBarItem] "文件" (x, y, w, h)
  ...

=== Dock 栏 ===
[AXList] "Dock" (x, y, w, h)
  [AXDockItem] "Finder" (x, y, w, h)
  ...
```

### 6.5 控件树的限制参数

```python
# accessibility.py
MAX_DEPTH = 10                     # 最大递归深度
前台应用最大元素数 = 500
其他可见应用每个最大元素数 = 200
最多获取其他可见应用数 = 5
```

### 6.6 控件树裁剪策略

**两个适配器都实现了 `_strip_old_ui_trees()` 方法**，在每次调用 API 前裁剪消息历史中的旧控件树：

- **只保留最近 2 次控件树**
- 其余控件树替换为 `[控件树已省略]` 占位文本
- 目的：**避免 token 爆炸**

识别方式：
- **Anthropic 适配器**：先从 assistant 消息中找到 `name == "accessibility"` 的 `tool_use` block 的 ID，再找对应的 `tool_result` 进行裁剪
- **OpenAI 适配器**：先从 assistant 消息的 `tool_calls` 中找到 `function.name == "accessibility"` 的 tool call ID，再找对应的 `role: "tool"` 消息进行裁剪

```python
# 调用链（与截图裁剪串联）
_call_model(messages)
  → trimmed = self._strip_old_ui_trees(messages, keep=2)  # 先裁剪控件树
  → trimmed = self._strip_old_images(trimmed)              # 再裁剪截图
  → 调用 API（用裁剪后的 messages）

# 注意：裁剪是对 deep copy 操作，不修改原始 messages 列表
```

---

## 7. 其他 Tool 结果的携带方式

### 7.1 操作成功

```python
# Anthropic
{"type": "tool_result", "tool_use_id": "xxx", "content": [{"type": "text", "text": "操作已执行成功。"}]}

# OpenAI
{"role": "tool", "tool_call_id": "xxx", "content": "{\"result\":\"操作执行成功。\"}"}
```

### 7.2 操作失败

```python
# Anthropic
{"type": "tool_result", "tool_use_id": "xxx", "content": [{"type": "text", "text": "Error: 错误信息"}], "is_error": True}

# OpenAI
{"role": "tool", "tool_call_id": "xxx", "content": "{\"error\":\"错误信息\"}"}
```

### 7.3 操作有返回数据

```python
# 如 cursor_position 等
# Anthropic
{"type": "tool_result", "tool_use_id": "xxx", "content": [{"type": "text", "text": "{\"x\": 100, \"y\": 200}"}]}

# OpenAI
{"role": "tool", "tool_call_id": "xxx", "content": "{\"result\":{\"x\":100,\"y\":200}}"}
```

---

## 8. 单次 API 调用的完整 payload 示意

### Anthropic

```json
{
  "model": "internal-model-opus-4-6-aws",
  "max_tokens": 4096,
  "system": "你是一个 AI 助手...屏幕尺寸: 3456 x 2234...",
  "tools": [
    {"name": "computer", "description": "...", "input_schema": {...}},
    {"name": "accessibility", "description": "...", "input_schema": {...}}
  ],
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "打开浏览器搜索 Python"}]},
    {"role": "assistant", "content": [{"type": "tool_use", ...}]},
    {"role": "user", "content": [{"type": "tool_result", ...}]},
    ...（所有历史消息，截图已裁剪为只保留最后 1 张）
  ]
}
```

### OpenAI

```json
{
  "model": "internal-model",
  "max_tokens": 4096,
  "messages": [
    {"role": "system", "content": "你是一个 AI 助手..."},
    {"role": "user", "content": "打开浏览器搜索 Python"},
    {"role": "assistant", "content": null, "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "...", "content": "..."},
    ...（所有历史消息，截图已裁剪为只保留最后 1 张）
  ],
  "tools": [
    {"type": "function", "function": {"name": "computer", ...}},
    {"type": "function", "function": {"name": "accessibility", ...}}
  ],
  "tool_choice": "auto"
}
```

---

## 9. Token 消耗估算

每次 API 调用的固定开销：

| 组件 | 估算大小 | 估算 token |
|------|---------|-----------|
| System Prompt | ~2K 字符 | ~500-800 |
| Tool 定义 (computer + accessibility) | ~2K 字符 | ~500-800 |
| **固定开销合计** | ~4K 字符 | **~1K-1.6K** |

每次 loop 的变量开销：

| 内容 | 估算大小 | 估算 token |
|------|---------|-----------|
| 截图 (base64 JPEG) | ~200-500K 字符 | 按图片 token 计费 |
| UI 控件树 | ~32K 字符 | ~8K-10K |
| 操作结果文本 | ~50-200 字符 | ~20-50 |
| AI 回复文本 | ~100-500 字符 | ~50-200 |

### 累积效应

由于消息历史是累积的，但截图和控件树都有裁剪策略，token 增长得到了控制：

- **截图**：只保留最后 1 张，其余替换为 `[截图已省略]`
- **控件树**：只保留最近 2 次（~64K 字符 ≈ 16K~20K tokens），其余替换为 `[控件树已省略]`
- **操作结果文本**：全部保留，但单条很小（~50-200 字符）

即使 AI 在一次任务中调用了 10 次 accessibility，消息历史中最多只保留 2 份控件树文本。

---

## 10. 总结

| 维度 | 当前状态 |
|------|---------|
| **跨任务上下文** | ❌ 不携带（每次新任务创建全新 session） |
| **同任务内上下文** | ✅ 完整累积（所有消息历史都保留） |
| **System Prompt** | ✅ 每次 API 调用都传（Anthropic 独立参数，OpenAI 在 messages[0]） |
| **Tool 定义** | ✅ 每次 API 调用都传 |
| **截图** | AI 主动请求才有；历史中只保留最后 1 张，旧截图替换为 `[截图已省略]` |
| **UI 控件树** | AI 主动请求才有；历史中**只保留最近 2 次**，旧控件树替换为 `[控件树已省略]` |
| **操作结果** | 历史中全部保留 |
