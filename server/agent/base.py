"""
Agent 会话基类 - 执行循环核心逻辑

截图 → 发送给 AI → 解析 tool_use → 执行动作 → 再截图 → 循环
支持暂停/恢复/停止控制
"""

import asyncio
import time
from enum import Enum
from typing import Callable, Awaitable, Optional, Any

from loguru import logger

from ..core.action_executor import ActionExecutor, ActionRequest, ActionResult, ActionType

# Agent 专用日志器
agent_log = logger.bind(agent=True)


class AgentState(str, Enum):
    """Agent 运行状态"""
    IDLE = "idle"           # 空闲，等待用户指令
    RUNNING = "running"     # 正在执行任务
    PAUSED = "paused"       # 已暂停
    STOPPED = "stopped"     # 已停止


class StepType(str, Enum):
    """执行步骤类型（推送给前端）"""
    SCREENSHOT = "screenshot"   # 正在截图
    THINKING = "thinking"       # AI 正在分析
    ACTION = "action"           # 正在执行动作
    TEXT = "text"               # AI 的文字回复
    ERROR = "error"             # 错误
    COMPLETE = "complete"       # 任务完成
    PAUSED = "paused"           # 已暂停
    RESUMED = "resumed"         # 已恢复


# Agent 适合调用的操作（排除流式拖拽等人工触控专用操作）
AGENT_ACTIONS = {
    ActionType.SCREENSHOT,
    ActionType.CLICK,
    ActionType.DOUBLE_CLICK,
    ActionType.RIGHT_CLICK,
    ActionType.MOVE,
    ActionType.DRAG,          # 一次性拖拽（Agent 用这个代替流式拖拽）
    ActionType.SCROLL,
    ActionType.TYPE,
    ActionType.KEY,
    ActionType.CURSOR_POSITION,
    ActionType.WAIT,
}

# 流式拖拽操作 - 仅供人工触控使用，Agent 不应调用
HUMAN_ONLY_ACTIONS = {
    ActionType.DRAG_START,
    ActionType.DRAG_MOVE,
    ActionType.DRAG_END,
}


class AgentSession:
    """AI Agent 执行会话

    管理一次 Agent 任务的完整生命周期：
    1. 用户发送任务描述
    2. Agent 截图 → 调用 AI → 解析动作 → 执行 → 循环
    3. 直到 AI 认为任务完成或用户中止

    事件回调 on_event(event: dict) 用于推送步骤更新到前端
    """

    # 每步操作后的等待时间（秒），确保 UI 动画完成
    POST_ACTION_DELAY = 0.8
    # 最大执行步骤数（防止无限循环）
    MAX_STEPS = 50
    # 截图参数
    SCREENSHOT_QUALITY = 70
    SCREENSHOT_MAX_SIZE = (1280, 800)
    # 坐标刻度尺参数（帮助 AI 精确定位）
    RULER_SIZE = 20          # 刻度尺宽度（像素）
    RULER_COLOR = (50, 50, 50)        # 刻度尺背景色（深灰）
    RULER_TEXT_COLOR = (220, 220, 220)  # 刻度文字颜色（浅灰）
    RULER_TICK_COLOR = (180, 180, 180)  # 刻度线颜色
    RULER_INTERVAL = 100     # 刻度间隔（像素）

    def __init__(
        self,
        executor: ActionExecutor,
        on_event: Callable[[dict], Awaitable[None]],
    ):
        """
        Args:
            executor: 统一动作执行器（复用主服务实例）
            on_event: 异步事件回调，推送步骤更新到前端
                      签名: async def on_event(event: dict)
        """
        self.executor = executor
        self.on_event = on_event
        self.state = AgentState.IDLE
        self.messages: list[dict] = []       # 对话历史
        self._task: Optional[asyncio.Task] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 初始未暂停
        self._step_count = 0

    async def _emit(self, step_type: StepType, data: dict | None = None):
        """发送事件到前端"""
        event = {
            "type": step_type.value,
            "step": self._step_count,
            "state": self.state.value,
            "timestamp": time.time(),
        }
        if data:
            event.update(data)
        try:
            await self.on_event(event)
        except Exception as e:
            logger.warning(f"事件推送失败: {e}")

    def take_screenshot(self) -> str:
        """截取全屏，绘制坐标刻度尺后返回 base64
        
        Agent 始终使用全屏截图，不受前端窗口模式影响。
        截图边缘绘制 X/Y 坐标刻度尺，帮助 AI 精确定位坐标。
        
        注意：刻度尺标注的是原始截图坐标（不含刻度尺偏移），
        AI 返回的坐标也是原始截图坐标。系统会自动处理偏移。
        """
        import io
        import base64
        from PIL import Image, ImageDraw, ImageFont

        # 截取全屏原始图
        img = self.executor.screen.capture_fullscreen_raw()
        # 缩放到 Agent 截图尺寸
        if self.SCREENSHOT_MAX_SIZE:
            img.thumbnail(self.SCREENSHOT_MAX_SIZE, Image.Resampling.LANCZOS)
        
        orig_w, orig_h = img.size
        ruler = self.RULER_SIZE

        # 创建带刻度尺的新画布（左侧 + 顶部各加 ruler 像素）
        new_w = orig_w + ruler
        new_h = orig_h + ruler
        canvas = Image.new("RGB", (new_w, new_h), self.RULER_COLOR)
        # 把原始截图粘贴到右下区域
        canvas.paste(img, (ruler, ruler))

        draw = ImageDraw.Draw(canvas)

        # 使用默认字体（小号）
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
        except Exception:
            font = ImageFont.load_default()

        interval = self.RULER_INTERVAL

        # 绘制顶部 X 轴刻度尺
        for x in range(0, orig_w, interval):
            # 刻度线
            draw.line([(ruler + x, ruler - 6), (ruler + x, ruler)], fill=self.RULER_TICK_COLOR, width=1)
            # 数字标签
            label = str(x)
            draw.text((ruler + x + 2, 2), label, fill=self.RULER_TEXT_COLOR, font=font)

        # 绘制左侧 Y 轴刻度尺
        for y in range(0, orig_h, interval):
            # 刻度线
            draw.line([(ruler - 6, ruler + y), (ruler, ruler + y)], fill=self.RULER_TICK_COLOR, width=1)
            # 数字标签（垂直方向）
            label = str(y)
            draw.text((1, ruler + y + 2), label, fill=self.RULER_TEXT_COLOR, font=font)

        # 编码为 JPEG base64
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=self.SCREENSHOT_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        agent_log.debug(
            f"[截图+刻度尺] 原始 {orig_w}x{orig_h} → 画布 {new_w}x{new_h}, "
            f"quality={self.SCREENSHOT_QUALITY}, base64_len={len(b64)}"
        )
        return b64

    def execute_action(self, req: ActionRequest) -> ActionResult:
        """执行一个操控动作（同步）
        
        Agent 仅允许调用 AGENT_ACTIONS 中的动作。
        使用 execute_absolute（物理屏幕绝对坐标），
        因为 Agent 截取的是全屏截图，坐标已通过 _scale_coord 
        映射为物理屏幕绝对坐标，不需要再叠加窗口偏移。
        """
        if req.action in HUMAN_ONLY_ACTIONS:
            return ActionResult(
                success=False,
                action=req.action.value,
                error=f"动作 {req.action.value} 仅供人工触控使用，Agent 请使用 drag 代替",
            )
        return self.executor.execute_absolute(req)

    # 需要操控屏幕的任务关键词（匹配到任意一个就需要截图）
    _SCREEN_ACTION_KEYWORDS = [
        # 鼠标操作
        "点击", "点一下", "点开", "打开", "关闭", "最小化", "最大化",
        "双击", "右键", "右击", "拖拽", "拖动", "拖放",
        # 键盘操作
        "输入", "填写", "键入", "敲入", "按下", "按键",
        "复制", "粘贴", "剪切", "撤销", "全选",
        # 滚动
        "滚动", "翻页", "向上滚", "向下滚", "滑动",
        # 应用操作
        "启动", "切换到", "切换窗口", "打开应用",
        "微信", "企业微信", "Safari", "Chrome", "浏览器",
        "Finder", "终端", "Terminal", "VSCode", "Xcode",
        # 屏幕相关
        "屏幕", "界面", "窗口", "桌面", "菜单", "Dock",
        "看看", "看一下", "显示的", "当前屏幕",
        "截图", "截屏",
        # 文件操作
        "文件夹", "目录", "保存", "另存为", "下载",
        # 网页操作
        "网页", "网站", "URL", "链接", "百度", "谷歌",
        "登录", "注册",
        # 英文关键词
        "click", "open", "close", "type", "scroll", "drag",
        "launch", "switch", "press",
    ]

    def _task_needs_screenshot(self, task: str) -> bool:
        """判断任务是否需要操控屏幕（需要先截图）
        
        通过关键词匹配判断：
        - 包含操控类关键词 → 需要截图
        - 纯聊天/问答 → 不需要截图（AI 可以随时通过 screenshot action 主动截图）
        
        即使判断不截图，AI 在需要时仍可通过 tool_use 主动请求截图，
        所以这里偏保守（宁可不截也不要每次都截），不会影响功能。
        """
        task_lower = task.lower()
        for keyword in self._SCREEN_ACTION_KEYWORDS:
            if keyword.lower() in task_lower:
                agent_log.debug(f"[任务分析] 匹配到关键词 '{keyword}'，需要截图")
                return True
        agent_log.debug(f"[任务分析] 未匹配到操控关键词，跳过初始截图")
        return False

    async def run(self, task: str) -> None:
        """启动 Agent 执行循环

        这是一个模板方法，子类需要实现 _run_loop() 来定义具体的 AI 交互逻辑

        Args:
            task: 用户的自然语言任务描述
        """
        if self.state == AgentState.RUNNING:
            logger.warning("Agent 正在运行中，忽略重复启动")
            return

        self.state = AgentState.RUNNING
        self._step_count = 0

        try:
            await self._emit(StepType.TEXT, {"content": f"收到任务：{task}"})

            # 根据任务内容判断是否需要初始截图
            # 需要操控电脑的任务才截图，纯聊天/问答类不截图
            needs_screenshot = self._task_needs_screenshot(task)
            screenshot_b64 = ""
            
            if needs_screenshot:
                self._step_count += 1
                await self._emit(StepType.SCREENSHOT, {"content": "正在截取屏幕..."})
                screenshot_b64 = await asyncio.to_thread(self.take_screenshot)
                await self._emit(StepType.SCREENSHOT, {
                    "content": "屏幕截图完成",
                    "screenshot": screenshot_b64,
                })
            else:
                agent_log.info(f"[跳过初始截图] 任务不需要操控屏幕: {task[:80]}")

            # 进入 AI 执行循环（子类实现）
            await self._run_loop(task, screenshot_b64)

        except asyncio.CancelledError:
            logger.info("Agent 任务被取消")
            self.state = AgentState.STOPPED
            await self._emit(StepType.COMPLETE, {"content": "任务已停止"})
        except Exception as e:
            logger.error(f"Agent 执行异常: {e}")
            self.state = AgentState.IDLE
            await self._emit(StepType.ERROR, {"content": f"执行异常：{str(e)}"})
        else:
            if self.state == AgentState.RUNNING:
                self.state = AgentState.IDLE
                await self._emit(StepType.COMPLETE, {"content": "任务完成"})

    async def _run_loop(self, task: str, initial_screenshot: str) -> None:
        """AI 执行循环 — 子类必须实现

        Args:
            task: 用户任务描述
            initial_screenshot: 初始截图 base64
        """
        raise NotImplementedError("子类必须实现 _run_loop()")

    async def _check_pause(self):
        """检查暂停状态，如果暂停则等待恢复"""
        if not self._pause_event.is_set():
            await self._emit(StepType.PAUSED, {"content": "已暂停，等待恢复..."})
            await self._pause_event.wait()
            await self._emit(StepType.RESUMED, {"content": "已恢复执行"})

    async def _execute_and_screenshot(self, req: ActionRequest) -> tuple[ActionResult, str]:
        """执行动作并截图（Agent 循环中的一步）

        Returns:
            (action_result, screenshot_base64)
        """
        # 检查暂停
        await self._check_pause()

        # 检查步骤限制
        self._step_count += 1
        if self._step_count > self.MAX_STEPS:
            raise RuntimeError(f"执行步骤已达上限 ({self.MAX_STEPS})，自动停止")

        # 推送动作开始事件
        action_desc = self._describe_action(req)
        agent_log.info(f"[执行动作] 第 {self._step_count}/{self.MAX_STEPS} 步: {action_desc}")
        await self._emit(StepType.ACTION, {
            "content": f"执行: {action_desc}",
            "action": req.action.value,
            "params": req.model_dump(exclude_defaults=True),
        })

        # 执行动作
        import time as _time
        _t0 = _time.monotonic()
        result = await asyncio.to_thread(self.execute_action, req)
        _elapsed = _time.monotonic() - _t0

        if not result.success:
            agent_log.error(f"[动作失败] {action_desc} → 耗时 {_elapsed:.3f}s, error={result.error}")
            await self._emit(StepType.ERROR, {
                "content": f"动作执行失败: {result.error}",
            })
            return result, ""

        agent_log.debug(f"[动作成功] {action_desc} → 耗时 {_elapsed:.3f}s")

        # 等待 UI 动画完成
        await asyncio.sleep(self.POST_ACTION_DELAY)

        # 截图
        await self._emit(StepType.SCREENSHOT, {"content": "正在截取屏幕..."})
        screenshot_b64 = await asyncio.to_thread(self.take_screenshot)
        await self._emit(StepType.SCREENSHOT, {
            "content": "屏幕截图完成",
            "screenshot": screenshot_b64,
        })

        return result, screenshot_b64

    def _describe_action(self, req: ActionRequest) -> str:
        """生成动作的人类可读描述"""
        action = req.action
        if action == ActionType.CLICK:
            return f"点击 ({req.x}, {req.y})"
        elif action == ActionType.DOUBLE_CLICK:
            return f"双击 ({req.x}, {req.y})"
        elif action == ActionType.RIGHT_CLICK:
            return f"右键点击 ({req.x}, {req.y})"
        elif action == ActionType.MOVE:
            return f"移动鼠标到 ({req.x}, {req.y})"
        elif action == ActionType.DRAG:
            return f"拖拽 ({req.x},{req.y}) → ({req.end_x},{req.end_y})"
        elif action == ActionType.SCROLL:
            return f"滚动 {req.direction} ×{req.amount} @ ({req.x},{req.y})"
        elif action == ActionType.TYPE:
            text_preview = req.text[:30] + "..." if len(req.text) > 30 else req.text
            return f"输入文本: \"{text_preview}\""
        elif action == ActionType.KEY:
            return f"按键: {'+'.join(req.keys)}"
        elif action == ActionType.SCREENSHOT:
            return "截图"
        elif action == ActionType.CURSOR_POSITION:
            return "获取光标位置"
        elif action == ActionType.WAIT:
            return f"等待 {req.duration}s"
        else:
            return f"{action.value}"

    async def pause(self):
        """暂停 Agent"""
        if self.state == AgentState.RUNNING:
            self.state = AgentState.PAUSED
            self._pause_event.clear()
            logger.info("Agent 已暂停")

    async def resume(self):
        """恢复 Agent"""
        if self.state == AgentState.PAUSED:
            self.state = AgentState.RUNNING
            self._pause_event.set()
            logger.info("Agent 已恢复")

    async def stop(self):
        """停止 Agent"""
        self.state = AgentState.STOPPED
        self._pause_event.set()  # 如果在暂停中，先唤醒
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("Agent 任务已取消")

    def get_state(self) -> dict:
        """获取当前 Agent 状态"""
        return {
            "state": self.state.value,
            "step_count": self._step_count,
            "max_steps": self.MAX_STEPS,
            "message_count": len(self.messages),
        }
