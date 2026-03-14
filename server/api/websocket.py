"""
WebSocket 端点 - 画面推流和实时控制
支持：自适应帧率 / 增量传输(dirty region) / 光标位置推送 / 流式拖拽快速通道
"""

import asyncio
import json
import time
from typing import Optional

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from ..core.action_executor import ActionRequest, ActionExecutor, ActionType
from ..core.screen import DeltaEncoder

router = APIRouter()

# 全局引用
executor: ActionExecutor = None

# 全局活跃状态：有操控动作时提升帧率
_last_action_time: float = 0.0


def set_executor(exec_instance):
    global executor
    executor = exec_instance


class ConnectionManager:
    """WebSocket 连接管理"""

    def __init__(self):
        self.screen_connections: list[WebSocket] = []
        self.control_connections: list[WebSocket] = []

    async def connect_screen(self, ws: WebSocket):
        await ws.accept()
        self.screen_connections.append(ws)
        logger.info(f"画面连接建立, 当前连接数: {len(self.screen_connections)}")

    async def connect_control(self, ws: WebSocket):
        await ws.accept()
        self.control_connections.append(ws)
        logger.info(f"控制连接建立, 当前连接数: {len(self.control_connections)}")

    def disconnect_screen(self, ws: WebSocket):
        if ws in self.screen_connections:
            self.screen_connections.remove(ws)
        logger.info(f"画面连接断开, 剩余: {len(self.screen_connections)}")

    def disconnect_control(self, ws: WebSocket):
        if ws in self.control_connections:
            self.control_connections.remove(ws)
        logger.info(f"控制连接断开, 剩余: {len(self.control_connections)}")


manager = ConnectionManager()


# ───────── 自适应帧率控制 ─────────

class AdaptiveStreamer:
    """根据画面变化和用户操作动态调整帧率
    
    策略：
    - 无操作且画面静止 → 5 fps (省电省 CPU)
    - 有操作或画面变化 → 25 fps (流畅跟随)
    - 操作停止后 2 秒内保持高帧率，然后逐渐降低
    """

    FPS_IDLE = 5
    FPS_ACTIVE = 25
    ACTIVE_DURATION = 2.0  # 操作后保持高帧率的时间(秒)

    def __init__(self):
        self._prev_frame_sample: Optional[np.ndarray] = None
        self._current_fps = self.FPS_ACTIVE  # 初始高帧率（用户刚连接通常在操作）
        self._frame_changed = True

    @property
    def frame_interval(self) -> float:
        return 1.0 / self._current_fps

    @property
    def current_fps(self) -> int:
        return self._current_fps

    def update(self, jpeg_bytes: bytes) -> None:
        """每帧调用，根据画面变化和操作状态更新帧率"""
        global _last_action_time

        # 操作活跃检测
        now = time.time()
        action_active = (now - _last_action_time) < self.ACTIVE_DURATION

        if action_active:
            self._current_fps = self.FPS_ACTIVE
            return

        # 画面变化检测（采样前 2048 字节，快速近似对比）
        sample = np.frombuffer(jpeg_bytes[:2048], dtype=np.uint8)
        if self._prev_frame_sample is not None:
            diff = np.mean(np.abs(sample.astype(np.int16) - self._prev_frame_sample.astype(np.int16)))
            if diff > 3:
                self._current_fps = self.FPS_ACTIVE
            else:
                # 渐进降低：每次减少 2fps，最低到 IDLE
                self._current_fps = max(self.FPS_IDLE, self._current_fps - 2)
        self._prev_frame_sample = sample


# ───────── 画面推流（增量传输） ─────────

@router.websocket("/ws/screen")
async def screen_stream(websocket: WebSocket):
    """画面推流 WebSocket - 自适应帧率 + 增量传输 + 光标坐标推送
    
    增量传输协议：
    - 完整帧: 二进制消息 (JPEG bytes)
    - 增量帧: 文本消息 {"type": "delta", "regions": [...]}
    - 无变化: 文本消息 {"type": "skip"}
    - 光标:   文本消息 {"type": "cursor", "x":..., "y":..., "fps":...}
    """
    await manager.connect_screen(websocket)

    streamer = AdaptiveStreamer()
    delta_encoder = DeltaEncoder(quality=executor.screen.quality)
    cursor_push_counter = 0

    # 统计信息
    stats = {
        "full_frames": 0,
        "delta_frames": 0,
        "skip_frames": 0,
        "total_bytes_saved": 0,
    }
    stats_log_counter = 0

    try:
        while True:
            start = time.time()

            try:
                # 截屏并缩放
                img = executor.screen.capture_scaled()

                # 增量编码
                frame_type, payload = delta_encoder.encode(img)

                if frame_type == "full":
                    # 完整帧 → 二进制发送 (向后兼容)
                    jpeg_bytes = payload
                    streamer.update(jpeg_bytes)
                    await websocket.send_bytes(jpeg_bytes)
                    stats["full_frames"] += 1

                elif frame_type == "delta":
                    # 增量帧 → JSON 文本发送
                    # 用完整 JPEG 大小估算节省的字节数
                    await websocket.send_text(payload)
                    stats["delta_frames"] += 1
                    # 增量帧也需要更新帧率控制（使用空 bytes 标记有变化）
                    streamer.update(b'\x00' * 2048)

                elif frame_type == "skip":
                    # 无变化 → 发送 skip 标记
                    await websocket.send_text('{"type":"skip"}')
                    stats["skip_frames"] += 1
                    # 无变化时使用缓存 sample 保持帧率控制
                    streamer.update(streamer._prev_frame_sample.tobytes()[:2048] if streamer._prev_frame_sample is not None else b'\x00' * 2048)

                # 每 5 帧推送一次光标位置
                cursor_push_counter += 1
                if cursor_push_counter >= 5:
                    cursor_push_counter = 0
                    pos = executor.input_ctrl.get_cursor_position()
                    cursor_data = {
                        "type": "cursor",
                        "x": pos["x"],
                        "y": pos["y"],
                        "fps": streamer.current_fps,
                    }
                    # 窗口模式下附带窗口 bounds（窗口可能被拖动，需实时更新）
                    if executor.screen._window_id is not None and executor.screen._window_bounds:
                        cursor_data["bounds"] = executor.screen._window_bounds
                    await websocket.send_text(json.dumps(cursor_data))

                # 每 300 帧输出一次统计日志
                stats_log_counter += 1
                if stats_log_counter >= 300:
                    stats_log_counter = 0
                    total = stats["full_frames"] + stats["delta_frames"] + stats["skip_frames"]
                    if total > 0:
                        logger.info(
                            f"增量传输统计: 完整帧={stats['full_frames']} "
                            f"增量帧={stats['delta_frames']} "
                            f"跳过帧={stats['skip_frames']} "
                            f"节省率={((stats['delta_frames'] + stats['skip_frames']) / total * 100):.1f}%"
                        )
                    stats = {"full_frames": 0, "delta_frames": 0, "skip_frames": 0, "total_bytes_saved": 0}

            except Exception as e:
                logger.error(f"截屏发送失败: {e}")
                break

            elapsed = time.time() - start
            sleep_time = max(0, streamer.frame_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"画面流异常: {e}")
    finally:
        manager.disconnect_screen(websocket)


# ───────── 控制通道 ─────────

# 拖拽快速通道：drag_move 不回包，减少延迟
_FAST_ACTIONS = {ActionType.DRAG_MOVE}


@router.websocket("/ws/control")
async def control_channel(websocket: WebSocket):
    """控制通道 WebSocket - 接收操控指令，拖拽快速通道"""
    await manager.connect_control(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                req = ActionRequest(**msg)

                # 标记操作活跃（提升帧率）
                global _last_action_time
                _last_action_time = time.time()

                result = executor.execute(req)

                # 快速动作（如 drag_move 高频调用）不回包，减少延迟
                if req.action not in _FAST_ACTIONS:
                    await websocket.send_text(result.model_dump_json())

            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"success": False, "error": "JSON 解析失败"}))
            except Exception as e:
                logger.error(f"控制指令处理失败: {e}")
                await websocket.send_text(json.dumps({"success": False, "error": str(e)}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"控制通道异常: {e}")
    finally:
        manager.disconnect_control(websocket)
