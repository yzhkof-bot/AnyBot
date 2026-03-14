"""
WebRTC 画面传输模块
mss 截屏 → aiortc VideoTrack → H.264/VP8 编码 → 浏览器 <video> 播放

对比 MJPEG (WebSocket + 逐帧 JPEG):
- H.264 帧间压缩，同画质下带宽降低 5-10 倍
- 浏览器硬件解码，CPU 占用极低
- 延迟更低 (20-50ms vs 50-100ms)
- 画质更好，无 JPEG 块状伪影

信令流程 (通过 HTTP POST):
1. 前端创建 RTCPeerConnection → createOffer → POST /api/webrtc/offer
2. 服务端收到 offer → 添加 ScreenVideoTrack → createAnswer → 返回 answer
3. 前端 setRemoteDescription(answer) → 连接建立 → <video> 自动播放

控制指令通过 RTCDataChannel 传输 (比 WebSocket 延迟更低)
"""

import asyncio
import fractions
import json
import time
from typing import Optional

import numpy as np
from av import VideoFrame
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
)
from aiortc.contrib.media import MediaRelay
from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from loguru import logger

from ..core.action_executor import ActionExecutor, ActionRequest


# ───────── 画质优化：提升 aiortc H.264 编码器码率 ─────────
# aiortc 默认码率 1Mbps、上限 3Mbps，对 1728×1117 屏幕共享来说太低。
# 通过 monkey-patch 提升码率上限，并优化编码参数。
import aiortc.codecs.h264 as _h264_codec

# 提高码率：默认 3Mbps，上限 8Mbps
_h264_codec.DEFAULT_BITRATE = 3_000_000   # 3 Mbps（原 1 Mbps）
_h264_codec.MIN_BITRATE = 1_000_000       # 1 Mbps（原 500 kbps）
_h264_codec.MAX_BITRATE = 8_000_000       # 8 Mbps（原 3 Mbps）

# Monkey-patch _encode_frame 以优化编码选项
_original_encode_frame = _h264_codec.H264Encoder._encode_frame

def _patched_encode_frame(self, frame, force_keyframe):
    """优化 H.264 编码参数以提升屏幕共享画质"""
    import av as _av
    import fractions as _fractions

    if self.codec and (
        frame.width != self.codec.width
        or frame.height != self.codec.height
        or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1
    ):
        self.buffer_data = b""
        self.buffer_pts = None
        self.codec = None

    if force_keyframe:
        frame.pict_type = _av.video.frame.PictureType.I
    else:
        frame.pict_type = _av.video.frame.PictureType.NONE

    if self.codec is None:
        self.codec = _av.CodecContext.create("libx264", "w")
        self.codec.width = frame.width
        self.codec.height = frame.height
        self.codec.bit_rate = self.target_bitrate
        self.codec.pix_fmt = "yuv420p"
        self.codec.framerate = _fractions.Fraction(30, 1)
        self.codec.time_base = _fractions.Fraction(1, 30)
        self.codec.options = {
            "preset": "ultrafast",     # 极速编码，降低延迟
            "tune": "zerolatency",     # 零延迟模式
            "level": "4.1",            # 支持更高分辨率和码率
            "crf": "23",               # 质量模式（越低越清晰，23 是默认优质）
            "maxrate": str(self.target_bitrate),
            "bufsize": str(self.target_bitrate // 2),
        }
        self.codec.profile = "Baseline"
        logger.info(f"H.264 编码器初始化: {frame.width}x{frame.height}, "
                     f"bitrate={self.target_bitrate/1e6:.1f}Mbps, crf=23, preset=ultrafast")

    data_to_send = b""
    for package in self.codec.encode(frame):
        data_to_send += bytes(package)

    if data_to_send:
        yield from self._split_bitstream(data_to_send)

_h264_codec.H264Encoder._encode_frame = _patched_encode_frame


router = APIRouter(prefix="/api/webrtc")

# 全局引用
executor: ActionExecutor = None
# 活跃的 PeerConnection 集合
_peer_connections: set[RTCPeerConnection] = set()
# 活跃的 DataChannel 集合（用于服务端主动推送光标位置）
_data_channels: set = set()
# MediaRelay 用于多客户端共享同一视频源
_relay = MediaRelay()

# 全局活跃状态（与 websocket.py 共享）
_last_action_time: float = 0.0


def set_executor(exec_instance: ActionExecutor):
    global executor
    executor = exec_instance


# ───────── 自定义视频轨道：截屏 → VideoFrame ─────────

class ScreenVideoTrack(MediaStreamTrack):
    """将 mss 屏幕截图转换为 WebRTC 视频帧的自定义轨道

    aiortc 会调用 recv() 获取每一帧，然后通过 H.264/VP8 编码推送给浏览器。
    帧间压缩由 aiortc 的编码器自动处理 —— 这是比 MJPEG 质的飞跃。
    """

    kind = "video"

    # 目标帧率
    TARGET_FPS = 25
    # 自适应：无操作时降低帧率
    IDLE_FPS = 10
    ACTIVE_DURATION = 2.0  # 操作后保持高帧率的时间(秒)

    def __init__(self):
        super().__init__()
        self._start_time: Optional[float] = None
        self._frame_count = 0
        self._timestamp = 0
        self._cursor_push_counter = 0

    async def recv(self) -> VideoFrame:
        """每次被 aiortc 调用时截取一帧屏幕

        Returns:
            VideoFrame: av 库的视频帧对象，aiortc 会自动 H.264 编码
        """
        global _last_action_time

        if self._start_time is None:
            self._start_time = time.time()

        # 自适应帧率
        now = time.time()
        action_active = (now - _last_action_time) < self.ACTIVE_DURATION
        current_fps = self.TARGET_FPS if action_active else self.IDLE_FPS

        # 帧间隔控制 —— 基于实际时间而非帧计数，避免慢截屏导致帧堆积
        frame_interval = 1.0 / current_fps
        wait_time = self._next_frame_time - time.time() if hasattr(self, '_next_frame_time') else 0
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self._next_frame_time = time.time() + frame_interval

        # 截屏 → PIL Image → numpy → VideoFrame
        try:
            img = executor.screen.capture_scaled()
            arr = np.array(img)  # RGB uint8 (H, W, 3)
        except Exception as e:
            # 截屏失败时生成黑色帧，避免轨道中断
            logger.warning(f"截屏失败: {e}")
            arr = np.zeros((720, 1280, 3), dtype=np.uint8)

        # 创建 VideoFrame
        frame = VideoFrame.from_ndarray(arr, format="rgb24")

        # 设置时间戳（PTS）—— aiortc 需要这个来控制帧率和编码
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, 90000)  # 90kHz 时基（RTP 标准）
        self._timestamp += int(90000 / current_fps)

        self._frame_count += 1

        # 每 5 帧通过 DataChannel 推送光标位置
        self._cursor_push_counter += 1
        if self._cursor_push_counter >= 5 and _data_channels:
            self._cursor_push_counter = 0
            try:
                pos = executor.input_ctrl.get_cursor_position()
                cursor_data = {
                    "type": "cursor",
                    "x": pos["x"],
                    "y": pos["y"],
                    "fps": current_fps,
                }
                # 窗口模式下附带窗口 bounds
                if executor.screen._window_id is not None and executor.screen._window_bounds:
                    cursor_data["bounds"] = executor.screen._window_bounds
                cursor_msg = json.dumps(cursor_data)
                for ch in list(_data_channels):
                    try:
                        ch.send(cursor_msg)
                    except Exception:
                        _data_channels.discard(ch)
            except Exception:
                pass

        return frame


# ───────── 信令接口 ─────────

@router.post("/offer")
async def webrtc_offer(request: Request):
    """处理 WebRTC 信令：接收前端 offer，返回 answer

    流程：
    1. 前端 createOffer() → POST SDP 到这里
    2. 创建 RTCPeerConnection + ScreenVideoTrack
    3. setRemoteDescription(offer) → createAnswer()
    4. 返回 answer SDP → 前端 setRemoteDescription(answer)
    5. 连接建立，浏览器开始接收 H.264 视频流
    """
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    _peer_connections.add(pc)
    pc_id = f"PC-{id(pc) & 0xFFFF:04x}"

    logger.info(f"[{pc_id}] WebRTC 连接创建")

    # 添加屏幕视频轨道
    screen_track = ScreenVideoTrack()
    pc.addTrack(screen_track)
    logger.info(f"[{pc_id}] 添加 ScreenVideoTrack (H.264 编码)")

    # 监听连接状态
    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        logger.info(f"[{pc_id}] 连接状态: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            _peer_connections.discard(pc)
            logger.info(f"[{pc_id}] 连接已关闭, 剩余: {len(_peer_connections)}")

    # 监听 ICE 连接状态
    @pc.on("iceconnectionstatechange")
    async def on_ice_connection_state_change():
        logger.info(f"[{pc_id}] ICE 状态: {pc.iceConnectionState}")

    # 监听 DataChannel（控制指令通道）
    @pc.on("datachannel")
    def on_datachannel(channel):
        logger.info(f"[{pc_id}] DataChannel '{channel.label}' 已建立")
        _data_channels.add(channel)

        @channel.on("message")
        def on_message(message):
            _handle_datachannel_message(channel, message, pc_id)

        @channel.on("close")
        def on_close():
            _data_channels.discard(channel)
            logger.debug(f"[{pc_id}] DataChannel 已关闭，剩余: {len(_data_channels)}")

    # 处理 offer → 生成 answer
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logger.info(f"[{pc_id}] SDP answer 已生成")

    return JSONResponse({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    })


def _handle_datachannel_message(channel, message: str, pc_id: str):
    """处理通过 DataChannel 收到的控制指令

    DataChannel 延迟比 WebSocket 更低（走 SCTP over DTLS），
    适合高频操控指令（拖拽、实时移动等）。

    消息格式与 WebSocket 控制通道完全一致：
    {"action": "click", "x": 100, "y": 200, ...}
    """
    global _last_action_time

    try:
        msg = json.loads(message)

        # ping/pong 心跳
        if msg.get("type") == "ping":
            channel.send(json.dumps({"type": "pong", "ts": msg.get("ts", 0)}))
            return

        # 操控指令
        req = ActionRequest(**msg)
        _last_action_time = time.time()
        result = executor.execute(req)

        # 快速动作不回包（与 WebSocket 控制通道一致）
        from ..core.action_executor import ActionType
        _FAST_ACTIONS = {ActionType.DRAG_MOVE}
        if req.action not in _FAST_ACTIONS:
            channel.send(result.model_dump_json())

    except json.JSONDecodeError:
        channel.send(json.dumps({"success": False, "error": "JSON 解析失败"}))
    except Exception as e:
        logger.error(f"[{pc_id}] DataChannel 指令处理失败: {e}")
        channel.send(json.dumps({"success": False, "error": str(e)}))


# ───────── 状态查询 ─────────

@router.get("/status")
async def webrtc_status():
    """查询 WebRTC 连接状态"""
    connections = []
    for pc in _peer_connections:
        connections.append({
            "state": pc.connectionState,
            "ice_state": pc.iceConnectionState,
        })
    return {
        "active_connections": len(_peer_connections),
        "connections": connections,
    }


# ───────── 清理 ─────────

async def close_all():
    """关闭所有 WebRTC 连接（服务关闭时调用）"""
    coros = [pc.close() for pc in _peer_connections]
    await asyncio.gather(*coros)
    _peer_connections.clear()
    logger.info("所有 WebRTC 连接已关闭")
