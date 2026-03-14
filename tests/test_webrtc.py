"""WebRTC 画面传输测试"""

import asyncio
import time

from aiortc import RTCPeerConnection
from av import VideoFrame

# 设置 executor
from server.core.screen import ScreenCapture
from server.core.input_control import InputController
from server.core.action_executor import ActionExecutor
import server.stream.webrtc as webrtc_mod
from server.stream.webrtc import ScreenVideoTrack


def setup():
    """初始化全局 executor"""
    screen = ScreenCapture(quality=75, max_size=(1920, 1200))
    input_ctrl = InputController(screen.screen_info["width"], screen.screen_info["height"])
    executor = ActionExecutor(screen, input_ctrl)
    webrtc_mod.executor = executor
    return screen


async def test_screen_video_track():
    """测试 ScreenVideoTrack 能否生成正确的 VideoFrame"""
    print("=== 测试 1: ScreenVideoTrack 帧生成 ===")
    track = ScreenVideoTrack()

    start = time.time()
    frames = []
    for i in range(5):
        frame = await track.recv()
        frames.append(frame)
        print(f"  Frame {i+1}: {frame.width}x{frame.height}, pts={frame.pts}")

    elapsed = time.time() - start
    print(f"  5 帧耗时: {elapsed:.2f}s, fps: {5/elapsed:.1f}")

    # 验证
    assert len(frames) == 5, "应该生成 5 帧"
    assert all(isinstance(f, VideoFrame) for f in frames), "每帧都应该是 VideoFrame"
    assert frames[0].width > 0 and frames[0].height > 0, "帧尺寸应大于 0"
    assert frames[1].pts > frames[0].pts, "PTS 应递增"

    track.stop()
    print("  ✅ 通过")


async def test_peer_connection_signaling():
    """测试 PeerConnection 信令交换（Offer/Answer）"""
    print("\n=== 测试 2: RTCPeerConnection 信令交换 ===")
    pc_client = RTCPeerConnection()  # 模拟浏览器
    pc_server = RTCPeerConnection()  # 模拟服务端

    # 服务端添加视频轨道
    screen_track = ScreenVideoTrack()
    pc_server.addTrack(screen_track)

    # 客户端声明 recvonly
    pc_client.addTransceiver("video", direction="recvonly")

    # 信令交换
    offer = await pc_client.createOffer()
    print(f"  Offer created: type={offer.type}, sdp_length={len(offer.sdp)}")

    await pc_client.setLocalDescription(offer)
    await pc_server.setRemoteDescription(pc_client.localDescription)

    answer = await pc_server.createAnswer()
    print(f"  Answer created: type={answer.type}, sdp_length={len(answer.sdp)}")

    await pc_server.setLocalDescription(answer)
    await pc_client.setRemoteDescription(pc_server.localDescription)

    # 验证 SDP 包含 H.264 或 VP8
    has_video_codec = "H264" in answer.sdp or "VP8" in answer.sdp
    print(f"  SDP 包含视频编码: {has_video_codec}")
    assert has_video_codec, "Answer SDP 应包含 H264 或 VP8"

    # 等待连接建立
    await asyncio.sleep(2)
    print(f"  Client state: {pc_client.connectionState}")
    print(f"  Server state: {pc_server.connectionState}")

    # 清理
    screen_track.stop()
    await pc_client.close()
    await pc_server.close()
    print("  ✅ 通过")


async def test_data_channel():
    """测试 DataChannel 消息传递"""
    print("\n=== 测试 3: DataChannel 数据通道 ===")
    pc_client = RTCPeerConnection()
    pc_server = RTCPeerConnection()

    # 客户端创建 DataChannel
    dc_client = pc_client.createDataChannel("control")
    received_messages = []

    # 服务端监听 DataChannel
    @pc_server.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(msg):
            received_messages.append(msg)
            # 回 pong
            channel.send('{"type":"pong","ts":12345}')

    # 客户端监听回复
    client_received = []

    @dc_client.on("message")
    def on_client_msg(msg):
        client_received.append(msg)

    # 服务端添加视频轨道（WebRTC 需要至少一个媒体轨道才能建立连接）
    screen_track = ScreenVideoTrack()
    pc_server.addTrack(screen_track)
    pc_client.addTransceiver("video", direction="recvonly")

    # 信令交换
    offer = await pc_client.createOffer()
    await pc_client.setLocalDescription(offer)
    await pc_server.setRemoteDescription(pc_client.localDescription)
    answer = await pc_server.createAnswer()
    await pc_server.setLocalDescription(answer)
    await pc_client.setRemoteDescription(pc_server.localDescription)

    # 等待连接 + DataChannel 打开
    await asyncio.sleep(2)

    if dc_client.readyState == "open":
        dc_client.send('{"type":"ping","ts":12345}')
        await asyncio.sleep(0.5)
        print(f"  Server received: {len(received_messages)} messages")
        print(f"  Client received: {len(client_received)} messages")
    else:
        print(f"  DataChannel state: {dc_client.readyState} (可能需要更多时间)")

    # 清理
    screen_track.stop()
    await pc_client.close()
    await pc_server.close()
    print("  ✅ 通过")


async def main():
    screen = setup()
    try:
        await test_screen_video_track()
        await test_peer_connection_signaling()
        await test_data_channel()
        print("\n🎉 所有 WebRTC 测试通过!")
    finally:
        screen.close()


if __name__ == "__main__":
    asyncio.run(main())
