"""
Agent 会话基类 - 执行循环核心逻辑

AI 自行决定每步是否获取控件树/截图/执行操作，框架层不强制获取任何上下文。
支持暂停/恢复/停止控制
"""

import asyncio
import time
from enum import Enum
from typing import Callable, Awaitable, Optional, Any

from loguru import logger

from ..core.action_executor import ActionExecutor, ActionRequest, ActionResult, ActionType
from ..core.accessibility import get_accessibility_tree

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
        """截取全屏原始截图，直接编码返回 base64
        
        Agent 始终使用全屏截图，不受前端窗口模式影响。
        不做任何标注/叠加，AI 直接看原始屏幕内容。
        """
        import io
        import base64

        # 截取全屏原始图（不缩放，保持物理分辨率）
        img = self.executor.screen.capture_fullscreen_raw()
        orig_w, orig_h = img.size

        # 编码为 JPEG base64
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.SCREENSHOT_QUALITY, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        agent_log.debug(
            f"[截图] {orig_w}x{orig_h}, "
            f"quality={self.SCREENSHOT_QUALITY}, base64_len={len(b64)}"
        )
        return b64

    # 调试用：保留最近 N 次控件树结果到文件
    _UI_TREE_DEBUG_DIR = "debug_ui_trees"
    _UI_TREE_KEEP_COUNT = 5

    def get_ui_tree(self) -> str:
        """获取当前前台应用的 UI 控件树（Accessibility API）
        
        返回缩进文本格式的控件树，包含每个 UI 元素的角色、标题、坐标和尺寸。
        坐标与截图像素坐标一致，可直接用于 click 操作的 coordinate 参数。
        
        调试模式下会将最近 5 次控件树保存到 debug_ui_trees/ 目录，方便排查问题。
        
        Returns:
            控件树文本字符串
        """
        import time as _time
        _t0 = _time.monotonic()
        tree_text = get_accessibility_tree()
        _elapsed = _time.monotonic() - _t0
        
        # 统计信息
        line_count = tree_text.count('\n') + 1
        char_count = len(tree_text)
        agent_log.info(
            f"[控件树] 耗时 {_elapsed:.3f}s, "
            f"行数={line_count}, 字符数={char_count}"
        )
        
        # 保存到调试文件（最近 N 次）
        self._save_ui_tree_debug(tree_text, _elapsed)
        
        return tree_text

    def _save_ui_tree_debug(self, tree_text: str, elapsed: float) -> None:
        """将控件树保存到调试文件，保留最近 N 次结果
        
        文件命名：ui_tree_{序号}_{时间戳}.txt
        自动清理超出保留数量的旧文件。
        """
        import os
        from datetime import datetime
        
        try:
            # 确保调试目录存在（相对于项目根目录）
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            debug_dir = os.path.join(project_root, self._UI_TREE_DEBUG_DIR)
            os.makedirs(debug_dir, exist_ok=True)
            
            # 生成文件名（带时间戳方便排查）
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"ui_tree_{ts}.txt"
            filepath = os.path.join(debug_dir, filename)
            
            # 写入文件，头部附加统计信息
            line_count = tree_text.count('\n') + 1
            header = (
                f"# UI 控件树调试快照\n"
                f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# 耗时: {elapsed:.3f}s\n"
                f"# 行数: {line_count}, 字符数: {len(tree_text)}\n"
                f"# {'=' * 60}\n\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header + tree_text)
            
            agent_log.debug(f"[控件树调试] 已保存: {filepath}")
            
            # 清理旧文件，只保留最近 N 个
            existing = sorted(
                [f for f in os.listdir(debug_dir) if f.startswith("ui_tree_") and f.endswith(".txt")]
            )
            while len(existing) > self._UI_TREE_KEEP_COUNT:
                old_file = existing.pop(0)
                old_path = os.path.join(debug_dir, old_file)
                os.remove(old_path)
                agent_log.debug(f"[控件树调试] 已清理旧文件: {old_file}")
                
        except Exception as e:
            # 调试功能不影响主流程
            agent_log.warning(f"[控件树调试] 保存失败（不影响功能）: {e}")

    def execute_action(self, req: ActionRequest) -> ActionResult:
        """执行一个操控动作（同步）
        
        Agent 仅允许调用 AGENT_ACTIONS 中的动作。
        使用 execute_absolute（物理屏幕绝对坐标），
        因为 Agent 截取的是全屏截图，AI 返回的坐标就是物理屏幕绝对坐标。
        """
        if req.action in HUMAN_ONLY_ACTIONS:
            return ActionResult(
                success=False,
                action=req.action.value,
                error=f"动作 {req.action.value} 仅供人工触控使用，Agent 请使用 drag 代替",
            )
        return self.executor.execute_absolute(req)

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

            # 直接进入 AI 执行循环，让 AI 自行判断是否需要获取控件树或截图
            await self._run_loop(task)

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

    async def _run_loop(self, task: str) -> None:
        """AI 执行循环 — 子类必须实现

        AI 自行决定每一步是否需要获取控件树、截图、或直接操作。
        框架层不强制获取任何上下文信息。

        Args:
            task: 用户任务描述
        """
        raise NotImplementedError("子类必须实现 _run_loop()")

    async def _check_pause(self):
        """检查暂停状态，如果暂停则等待恢复"""
        if not self._pause_event.is_set():
            await self._emit(StepType.PAUSED, {"content": "已暂停，等待恢复..."})
            await self._pause_event.wait()
            await self._emit(StepType.RESUMED, {"content": "已恢复执行"})

    async def _execute_action(self, req: ActionRequest) -> ActionResult:
        """执行动作（Agent 循环中的一步）
        
        只负责执行操作并返回结果。不自动获取控件树或截图，
        由 AI 自行决定操作后是否需要获取上下文信息。

        Returns:
            ActionResult
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
            return result

        agent_log.debug(f"[动作成功] {action_desc} → 耗时 {_elapsed:.3f}s")

        # 等待 UI 动画完成
        await asyncio.sleep(self.POST_ACTION_DELAY)

        return result

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
