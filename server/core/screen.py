"""
屏幕捕获模块 - 使用 mss 进行高性能截屏
macOS 底层调用 Quartz API，性能优秀
支持增量传输：检测画面变化区域，仅推送 dirty region
"""

import io
import json
import base64
import time
from typing import Optional, Tuple, List

import numpy as np
import mss
import mss.tools
from PIL import Image
from loguru import logger


class ScreenCapture:
    """屏幕捕获器"""

    def __init__(self, monitor_index: int = 1, quality: int = 50, max_size: Tuple[int, int] = (1280, 800)):
        """
        Args:
            monitor_index: 显示器索引，1=主屏幕
            quality: JPEG 压缩质量 (1-100)
            max_size: 最大输出尺寸 (width, height)
        """
        self.monitor_index = monitor_index
        self.quality = quality
        self.max_size = max_size
        self._sct = mss.mss()
        self._screen_info = None
        self._update_screen_info()

    def _update_screen_info(self):
        """更新屏幕信息"""
        monitor = self._sct.monitors[self.monitor_index]
        self._screen_info = {
            "width": monitor["width"],
            "height": monitor["height"],
            "left": monitor["left"],
            "top": monitor["top"],
        }
        logger.info(f"屏幕信息: {self._screen_info}")

    @property
    def screen_info(self) -> dict:
        return self._screen_info.copy()

    @property
    def screen_size(self) -> Tuple[int, int]:
        return (self._screen_info["width"], self._screen_info["height"])

    def capture_raw(self) -> Image.Image:
        """捕获原始屏幕图像，返回 PIL Image"""
        monitor = self._sct.monitors[self.monitor_index]
        sct_img = self._sct.grab(monitor)
        # mss 返回 BGRA，转为 RGB
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        return img

    def capture_scaled(self, max_size: Optional[Tuple[int, int]] = None) -> Image.Image:
        """捕获屏幕并缩放，返回 PIL Image"""
        max_size = max_size or self.max_size
        img = self.capture_raw()
        if max_size:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)
        return img

    def capture_jpeg(self, quality: Optional[int] = None, max_size: Optional[Tuple[int, int]] = None) -> bytes:
        """捕获屏幕并返回 JPEG bytes"""
        quality = quality or self.quality
        max_size = max_size or self.max_size

        img = self.capture_raw()

        # 按比例缩放
        if max_size:
            img.thumbnail(max_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    def capture_base64(self, quality: Optional[int] = None, max_size: Optional[Tuple[int, int]] = None) -> str:
        """捕获屏幕并返回 base64 编码的 JPEG（供 AI Agent 使用）"""
        jpeg_bytes = self.capture_jpeg(quality=quality, max_size=max_size)
        return base64.b64encode(jpeg_bytes).decode("utf-8")

    def benchmark(self, frames: int = 30) -> dict:
        """性能测试"""
        start = time.time()
        total_bytes = 0
        for _ in range(frames):
            data = self.capture_jpeg()
            total_bytes += len(data)
        elapsed = time.time() - start
        fps = frames / elapsed
        avg_size = total_bytes / frames
        logger.info(f"截屏性能: {fps:.1f} fps, 平均帧大小: {avg_size/1024:.1f} KB, 总耗时: {elapsed:.2f}s")
        return {"fps": fps, "avg_frame_size_kb": avg_size / 1024, "elapsed": elapsed}

    def close(self):
        self._sct.close()


# ────────── 增量传输引擎 ──────────

class DeltaEncoder:
    """增量帧编码器 — 检测画面变化区域，仅推送 dirty regions
    
    工作流程：
    1. 将当前帧与上一帧做像素级对比（缩略图快速检测）
    2. 画面无变化 → 返回 skip 标记
    3. 有变化 → 划分网格，找出变化的块 → 合并相邻块为 dirty region
    4. 变化面积 > 50% → 直接发送完整帧（增量编码收益低）
    5. 变化面积 ≤ 50% → 仅裁剪变化区域为 JPEG 发送

    协议格式（通过 WebSocket 文本消息发送）：
    - 无变化：{"type": "skip"}
    - 增量帧：{"type": "delta", "regions": [{"x":..,"y":..,"w":..,"h":..,"data":"base64-jpeg"}, ...]}
    - 完整帧：直接发送二进制 JPEG bytes（与现有逻辑兼容）
    """

    # 网格块大小（像素），越小精度越高但开销越大
    BLOCK_SIZE = 64
    # 像素差异阈值（0-255），低于此认为无变化
    DIFF_THRESHOLD = 8
    # 网格块内变化像素占比超过此值才认为该块变化
    BLOCK_CHANGE_RATIO = 0.05
    # 变化面积超过画面 50% 时直接发完整帧
    FULL_FRAME_RATIO = 0.50
    # 每隔多少帧强制发一次完整帧（防止累积误差）
    KEYFRAME_INTERVAL = 60

    def __init__(self, quality: int = 50):
        self.quality = quality
        self._prev_array: Optional[np.ndarray] = None
        self._frame_count = 0

    def reset(self):
        """重置状态（连接断开/重连时调用）"""
        self._prev_array = None
        self._frame_count = 0

    def encode(self, img: Image.Image) -> Tuple[str, object]:
        """对一帧图像进行增量编码

        Args:
            img: 已缩放的 PIL Image (RGB)

        Returns:
            (frame_type, payload):
            - ("full", jpeg_bytes): 完整帧，payload 是二进制 JPEG
            - ("skip", None): 无变化
            - ("delta", json_str): 增量帧，payload 是 JSON 字符串
        """
        self._frame_count += 1
        current = np.array(img, dtype=np.uint8)

        # 首帧或关键帧间隔 → 强制完整帧
        if self._prev_array is None or self._frame_count % self.KEYFRAME_INTERVAL == 0:
            self._prev_array = current
            return ("full", self._to_jpeg(img))

        # 快速全局差异检测（对比采样行加速）
        if current.shape != self._prev_array.shape:
            # 分辨率变化，发完整帧
            self._prev_array = current
            return ("full", self._to_jpeg(img))

        # 逐块比较
        h, w = current.shape[:2]
        bs = self.BLOCK_SIZE
        rows = (h + bs - 1) // bs
        cols = (w + bs - 1) // bs

        changed_blocks = []
        total_blocks = rows * cols

        for r in range(rows):
            for c in range(cols):
                y1 = r * bs
                y2 = min(y1 + bs, h)
                x1 = c * bs
                x2 = min(x1 + bs, w)

                curr_block = current[y1:y2, x1:x2]
                prev_block = self._prev_array[y1:y2, x1:x2]

                # 计算差异
                diff = np.abs(curr_block.astype(np.int16) - prev_block.astype(np.int16))
                changed_pixels = np.mean(diff, axis=2) > self.DIFF_THRESHOLD
                change_ratio = np.mean(changed_pixels)

                if change_ratio > self.BLOCK_CHANGE_RATIO:
                    changed_blocks.append((r, c, x1, y1, x2, y2))

        # 无变化
        if not changed_blocks:
            return ("skip", None)

        # 变化面积比
        change_area_ratio = len(changed_blocks) / total_blocks

        # 超过阈值 → 完整帧
        if change_area_ratio > self.FULL_FRAME_RATIO:
            self._prev_array = current
            return ("full", self._to_jpeg(img))

        # 合并相邻变化块为矩形区域，减少 region 数量
        regions = self._merge_blocks(changed_blocks, w, h)

        # 编码每个 dirty region 为 JPEG
        region_list = []
        for (rx, ry, rw, rh) in regions:
            region_img = img.crop((rx, ry, rx + rw, ry + rh))
            buf = io.BytesIO()
            region_img.save(buf, format="JPEG", quality=self.quality)
            region_data = base64.b64encode(buf.getvalue()).decode("utf-8")
            region_list.append({
                "x": rx, "y": ry, "w": rw, "h": rh,
                "data": region_data,
            })

        self._prev_array = current

        payload = json.dumps({"type": "delta", "regions": region_list})
        return ("delta", payload)

    def _merge_blocks(self, blocks: List[Tuple], img_w: int, img_h: int) -> List[Tuple[int, int, int, int]]:
        """将分散的变化块合并为更少的矩形区域
        
        策略：按行扫描，合并同行连续块，再纵向合并相同列范围的行
        返回 [(x, y, w, h), ...]
        """
        if not blocks:
            return []

        bs = self.BLOCK_SIZE

        # 建立网格 → 标记变化块
        max_r = max(b[0] for b in blocks)
        max_c = max(b[1] for b in blocks)
        grid = set((b[0], b[1]) for b in blocks)

        # 贪心合并：找连通矩形区域
        visited = set()
        regions = []

        for (r, c, x1, y1, x2, y2) in blocks:
            if (r, c) in visited:
                continue

            # 从 (r, c) 开始，尽量向右扩展
            c_end = c
            while (r, c_end + 1) in grid and (r, c_end + 1) not in visited:
                c_end += 1

            # 向下扩展，要求每行的 c~c_end 全部是变化块
            r_end = r
            expanding = True
            while expanding:
                next_r = r_end + 1
                for cc in range(c, c_end + 1):
                    if (next_r, cc) not in grid or (next_r, cc) in visited:
                        expanding = False
                        break
                if expanding:
                    r_end = next_r

            # 标记已访问
            for rr in range(r, r_end + 1):
                for cc in range(c, c_end + 1):
                    visited.add((rr, cc))

            # 计算像素坐标
            rx = c * bs
            ry = r * bs
            rw = min((c_end + 1) * bs, img_w) - rx
            rh = min((r_end + 1) * bs, img_h) - ry
            regions.append((rx, ry, rw, rh))

        return regions

    def _to_jpeg(self, img: Image.Image) -> bytes:
        """PIL Image → JPEG bytes"""
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self.quality, optimize=True)
        return buf.getvalue()
