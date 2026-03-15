"""
Agent 聊天 WebSocket API

前端通过 /ws/agent 连接，发送用户消息和控制指令，
接收 Agent 步骤更新（截图中/分析中/操作中、AI 回复、执行结果）。

协议格式（JSON）：

客户端 → 服务端：
  {"type": "chat", "content": "打开浏览器搜索 Python", "model": "internal-model-opus-4-6-aws"}
  {"type": "pause"}                                        # 暂停
  {"type": "resume"}                                       # 恢复
  {"type": "stop"}                                         # 停止
  {"type": "set_model", "model": "internal-model-opus-4-6-aws"}  # 切换模型

服务端 → 客户端：
  {"type": "screenshot", "step": 1, "state": "running", "screenshot": "base64..."}
  {"type": "thinking", "step": 2, "state": "running", "content": "AI 正在分析..."}
  {"type": "action", "step": 3, "state": "running", "content": "点击 (500, 300)"}
  {"type": "text", "step": 4, "state": "running", "content": "我已经帮你打开了..."}
  {"type": "complete", "step": 5, "state": "idle", "content": "任务完成"}
  {"type": "error", ..., "content": "错误信息"}
  {"type": "state", "state": "idle"|"running"|"paused"|"stopped"}

REST API：
  GET /api/agent/models — 获取可用模型列表
"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from ..core.action_executor import ActionExecutor
from .anthropic_adapter import AnthropicComputerUseAdapter, get_available_models, get_model_info, DEFAULT_MODEL_ID
from .base import AgentState

# Agent 专用日志器
agent_log = logger.bind(agent=True)

router = APIRouter(tags=["agent"])

# 全局引用，在 main.py 中注入
executor = None
# 当前选中的模型 ID（WebSocket 级别可覆盖）
current_model_id = DEFAULT_MODEL_ID


def set_executor(exec_instance: ActionExecutor):
    global executor
    executor = exec_instance


@router.get("/api/agent/models")
async def list_models():
    """获取可用的 AI 模型列表"""
    models = get_available_models()
    return {
        "models": models,
        "current": current_model_id,
    }


@router.websocket("/ws/agent")
async def agent_websocket(ws: WebSocket):
    """Agent 聊天 WebSocket 端点

    一个 WebSocket 连接对应一个 AgentSession。
    连接断开时自动停止 Agent。
    """
    await ws.accept()
    logger.info("Agent WebSocket 已连接")

    session = None
    agent_task = None
    ws_model_id = current_model_id  # 此连接的模型选择

    async def on_event(event: dict):
        """Agent 事件回调 → 推送到 WebSocket"""
        try:
            await ws.send_json(event)
        except Exception as e:
            logger.warning(f"WebSocket 推送失败: {e}")

    try:
        # 发送初始状态（包含模型列表）
        await ws.send_json({
            "type": "state",
            "state": AgentState.IDLE.value,
            "current_model": ws_model_id,
        })

        while True:
            # 接收客户端消息
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await ws.send_json({
                    "type": "error",
                    "content": "消息格式错误，需要 JSON",
                })
                continue

            msg_type = msg.get("type", "")
            agent_log.debug(f"[WS 收到] type={msg_type}, data={json.dumps(msg, ensure_ascii=False)[:200]}")

            if msg_type == "chat":
                # 用户发送任务
                content = msg.get("content", "").strip()
                if not content:
                    await ws.send_json({
                        "type": "error",
                        "content": "任务内容不能为空",
                    })
                    continue

                # 检查是否已在运行
                if session and session.state == AgentState.RUNNING:
                    await ws.send_json({
                        "type": "error",
                        "content": "Agent 正在执行任务，请先停止当前任务",
                    })
                    continue

                # 创建新的 Agent 会话
                if executor is None:
                    await ws.send_json({
                        "type": "error",
                        "content": "服务未初始化",
                    })
                    continue

                session = AnthropicComputerUseAdapter(
                    executor=executor,
                    on_event=on_event,
                    model_id=msg.get("model") or ws_model_id,
                )

                # 通知前端当前使用的模型
                used_model = msg.get("model") or ws_model_id
                model_info = get_model_info(used_model)
                model_name = model_info["name"] if model_info else used_model
                
                logger.info(f"Agent 开始任务: {content[:50]}... (model={used_model})")
                agent_log.info(
                    f"[用户指令] 任务: {content}, "
                    f"模型: {model_name} ({used_model})"
                )

                # 启动 Agent（异步任务，不阻塞 WebSocket 接收）
                agent_task = asyncio.create_task(session.run(content))
                session._task = agent_task

                # 监听任务完成（异步通知）
                async def _watch_task(task: asyncio.Task, sess: AnthropicComputerUseAdapter):
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        logger.error(f"Agent 任务异常: {e}")
                    finally:
                        # 任务结束后更新状态
                        if sess.state == AgentState.RUNNING:
                            sess.state = AgentState.IDLE
                        try:
                            await ws.send_json({
                                "type": "state",
                                "state": sess.state.value,
                            })
                        except Exception:
                            pass

                asyncio.create_task(_watch_task(agent_task, session))

            elif msg_type == "pause":
                if session and session.state == AgentState.RUNNING:
                    await session.pause()
                    await ws.send_json({
                        "type": "state",
                        "state": session.state.value,
                    })

            elif msg_type == "resume":
                if session and session.state == AgentState.PAUSED:
                    await session.resume()
                    await ws.send_json({
                        "type": "state",
                        "state": session.state.value,
                    })

            elif msg_type == "stop":
                if session and session.state in (AgentState.RUNNING, AgentState.PAUSED):
                    await session.stop()
                    await ws.send_json({
                        "type": "state",
                        "state": session.state.value,
                    })

            elif msg_type == "get_state":
                state_info = session.get_state() if session else {
                    "state": AgentState.IDLE.value,
                    "step_count": 0,
                    "max_steps": 50,
                    "message_count": 0,
                }
                await ws.send_json({
                    "type": "state_info",
                    **state_info,
                })

            elif msg_type == "set_model":
                new_model = msg.get("model", "")
                if new_model and get_model_info(new_model):
                    ws_model_id = new_model
                    model_info = get_model_info(new_model)
                    logger.info(f"Agent 切换模型: {new_model} ({model_info['name']})")
                    await ws.send_json({
                        "type": "model_changed",
                        "model": new_model,
                        "model_name": model_info["name"],
                    })
                else:
                    await ws.send_json({
                        "type": "error",
                        "content": f"未知模型: {new_model}",
                    })

            elif msg_type == "get_models":
                await ws.send_json({
                    "type": "models",
                    "models": get_available_models(),
                    "current": ws_model_id,
                })

            else:
                await ws.send_json({
                    "type": "error",
                    "content": f"未知消息类型: {msg_type}",
                })

    except WebSocketDisconnect:
        logger.info("Agent WebSocket 已断开")
    except Exception as e:
        logger.error(f"Agent WebSocket 异常: {e}")
    finally:
        # 清理：停止正在运行的 Agent
        if session and session.state in (AgentState.RUNNING, AgentState.PAUSED):
            await session.stop()
        if agent_task and not agent_task.done():
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Agent WebSocket 清理完成")
