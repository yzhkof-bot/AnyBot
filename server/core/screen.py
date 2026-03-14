"""
屏幕捕获模块 - 使用 mss 进行高性能截屏
macOS 底层调用 Quartz API，性能优秀
支持增量传输：检测画面变化区域，仅推送 dirty region
支持窗口捕获模式：通过 Quartz CGWindowListCreateImage 只截取指定窗口
"""

import io
import json
import base64
import time
import platform
from typing import Optional, Tuple, List

import numpy as np
import mss
import mss.tools
from PIL import Image
from loguru import logger

# macOS 窗口捕获支持
_HAS_QUARTZ = False
if platform.system() == "Darwin":
    try:
        import Quartz
        from Quartz import (
            CGWindowListCopyWindowInfo,
            CGWindowListCreateImage,
            CGRectNull,
            kCGWindowListOptionIncludingWindow,
            kCGWindowImageBoundsIgnoreFraming,
            kCGWindowImageNominalResolution,
            kCGWindowListOptionAll,
            kCGNullWindowID,
        )
        # CoreGraphics 在 pyobjc 中是 Quartz.CoreGraphics，不是独立模块
        from Quartz import CoreGraphics
        _HAS_QUARTZ = True
        logger.info("Quartz 窗口捕获支持已加载")
    except ImportError as e:
        logger.warning(f"Quartz 不可用，窗口捕获功能不可用: {e}")


class ScreenCapture:
    """屏幕捕获器 — 支持全屏模式和窗口模式"""

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
        # 窗口捕获模式
        self._window_id: Optional[int] = None       # None = 全屏模式
        self._window_name: Optional[str] = None      # 当前捕获的窗口名
        self._window_owner: Optional[str] = None     # 当前捕获的窗口所属应用
        self._window_bounds: Optional[dict] = None   # 窗口在屏幕上的位置 {x, y, w, h}
        # 置顶窗口列表 [{id, owner, name}, ...]
        self._pinned_windows: List[dict] = []
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
        info = self._screen_info.copy()
        info["window_mode"] = self._window_id is not None
        if self._window_id is not None:
            info["window_id"] = self._window_id
            info["window_name"] = self._window_name
            info["window_owner"] = self._window_owner
        return info

    @property
    def screen_size(self) -> Tuple[int, int]:
        return (self._screen_info["width"], self._screen_info["height"])

    # ───────── 窗口管理 ─────────

    def list_windows(self) -> List[dict]:
        """列出所有可见窗口（供前端选择）
        
        Quartz 的 CGWindowListCopyWindowInfo 返回的窗口天然按 Z-order 排列，
        最前面的（当前焦点）窗口排在列表最前。
        
        排序规则：置顶窗口 → 焦点窗口 → 其他窗口
        
        Returns:
            [{"id": 窗口ID, "owner": 应用名, "name": 窗口标题, 
              "bounds": {x,y,w,h}, "order": 0起始序号, "pinned": bool}, ...]
        """
        if not _HAS_QUARTZ:
            return []

        window_list = CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )

        pinned_ids = {p["id"] for p in self._pinned_windows}
        all_windows = []
        order = 0
        for win in window_list:
            # 过滤掉不可见的和太小的窗口
            layer = win.get("kCGWindowLayer", 0)
            alpha = win.get("kCGWindowAlpha", 0)
            if layer != 0 or alpha < 0.1:
                continue

            bounds = win.get("kCGWindowBounds", {})
            w = bounds.get("Width", 0)
            h = bounds.get("Height", 0)
            if w < 50 or h < 50:
                continue

            owner = win.get("kCGWindowOwnerName", "")
            name = win.get("kCGWindowName", "")
            wid = win.get("kCGWindowNumber", 0)

            # 过滤掉 AnyBot 自身和系统窗口
            if owner in ("Window Server", "SystemUIServer", "Dock"):
                continue

            all_windows.append({
                "id": wid,
                "owner": owner,
                "name": name or "(无标题)",
                "bounds": {
                    "x": int(bounds.get("X", 0)),
                    "y": int(bounds.get("Y", 0)),
                    "w": int(w),
                    "h": int(h),
                },
                "z_order": order,
                "pinned": wid in pinned_ids,
            })
            order += 1

        # 清理已不存在的置顶窗口
        current_ids = {w["id"] for w in all_windows}
        self._pinned_windows = [p for p in self._pinned_windows if p["id"] in current_ids]

        # 排序：置顶窗口（按置顶顺序）→ 非置顶窗口（按 Z-order）
        pinned_list = []
        normal_list = []
        for w in all_windows:
            if w["pinned"]:
                pinned_list.append(w)
            else:
                normal_list.append(w)

        # 置顶窗口按照用户添加的顺序排列
        pinned_order = {p["id"]: i for i, p in enumerate(self._pinned_windows)}
        pinned_list.sort(key=lambda w: pinned_order.get(w["id"], 0))

        result = pinned_list + normal_list
        # 重新编号 order
        for i, w in enumerate(result):
            w["order"] = i

        return result

    def pin_window(self, window_id: int, owner: str = "", name: str = "") -> bool:
        """置顶一个窗口
        
        Args:
            window_id: 窗口 ID
            owner: 应用名
            name: 窗口标题
            
        Returns:
            True 表示成功，False 表示已置顶
        """
        for p in self._pinned_windows:
            if p["id"] == window_id:
                return False  # 已置顶
        self._pinned_windows.append({"id": window_id, "owner": owner, "name": name})
        logger.info(f"置顶窗口: [{owner}] {name} (ID={window_id})")
        return True

    def unpin_window(self, window_id: int) -> bool:
        """取消置顶一个窗口
        
        Args:
            window_id: 窗口 ID
            
        Returns:
            True 表示成功，False 表示未置顶
        """
        before_len = len(self._pinned_windows)
        self._pinned_windows = [p for p in self._pinned_windows if p["id"] != window_id]
        removed = len(self._pinned_windows) < before_len
        if removed:
            logger.info(f"取消置顶窗口: ID={window_id}")
        return removed

    @property
    def pinned_window_ids(self) -> List[int]:
        """获取所有置顶的窗口 ID 列表"""
        return [p["id"] for p in self._pinned_windows]

    def set_window(self, window_id: Optional[int], window_name: str = "", window_owner: str = ""):
        """切换到窗口捕获模式或全屏模式
        
        Args:
            window_id: 窗口ID，None 表示切回全屏模式
            window_name: 窗口标题（记录用）
            window_owner: 应用名（记录用）
        """
        if window_id is None:
            # 切回全屏模式
            self._window_id = None
            self._window_name = None
            self._window_owner = None
            self._window_bounds = None
            # 恢复全屏幕尺寸
            self._update_screen_info()
            logger.info("切换到全屏捕获模式")
        else:
            if not _HAS_QUARTZ:
                logger.error("Quartz 不可用，无法切换到窗口模式")
                return
            self._window_id = window_id
            self._window_name = window_name
            self._window_owner = window_owner
            # 获取窗口的屏幕位置 (bounds)
            self._window_bounds = self._get_window_bounds(window_id)
            # 捕获一帧来更新 screen_info 中的尺寸
            try:
                img = self._capture_window()
                if img:
                    self._screen_info["width"] = img.width
                    self._screen_info["height"] = img.height
                    logger.info(f"切换到窗口模式: [{window_owner}] {window_name} "
                                f"(ID={window_id}, {img.width}x{img.height}, "
                                f"bounds={self._window_bounds})")
                else:
                    logger.warning(f"窗口 {window_id} 捕获失败，可能已关闭")
                    self._window_id = None
                    self._window_bounds = None
            except Exception as e:
                logger.error(f"窗口模式切换失败: {e}")
                self._window_id = None
                self._window_bounds = None

    def _get_window_bounds(self, window_id: int) -> Optional[dict]:
        """获取指定窗口在屏幕上的位置和尺寸"""
        if not _HAS_QUARTZ:
            return None
        window_list = CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )
        for win in window_list:
            if win.get("kCGWindowNumber", 0) == window_id:
                bounds = win.get("kCGWindowBounds", {})
                return {
                    "x": int(bounds.get("X", 0)),
                    "y": int(bounds.get("Y", 0)),
                    "w": int(bounds.get("Width", 0)),
                    "h": int(bounds.get("Height", 0)),
                }
        return None

    def get_window_offset(self) -> Tuple[int, int]:
        """获取当前窗口在屏幕上的左上角偏移坐标
        
        全屏模式下返回 (0, 0)
        窗口模式下返回 (window_x, window_y)，用于将窗口内相对坐标转换为屏幕绝对坐标
        """
        if self._window_id is None or self._window_bounds is None:
            return (0, 0)
        # 每次获取时刷新窗口位置（窗口可能被拖动）
        bounds = self._get_window_bounds(self._window_id)
        if bounds:
            self._window_bounds = bounds
            return (bounds["x"], bounds["y"])
        return (self._window_bounds["x"], self._window_bounds["y"])

    def activate_window(self) -> str:
        """将当前窗口模式的目标窗口激活到前台
        
        macOS 的鼠标/键盘事件会发送到屏幕绝对坐标处最前面的窗口，
        如果目标窗口被遮挡，操作会发送到错误的窗口。
        因此在执行操作前需要先将目标窗口提升到前台。
        
        优化：
        - 检查窗口是否已在 Z-order 最前（z_order=0），已在最前则跳过
        - 时间节流：0.5 秒内不重复激活（避免拖拽等高频操作卡顿）
        
        Returns:
            "already_front" — 窗口已在前台，无需操作
            "throttled" — 节流期内，跳过激活
            "activated" — 刚执行了激活操作（调用方应等待 macOS 完成切换）
            "failed" — 激活失败
        """
        if self._window_id is None or self._window_owner is None:
            return "already_front"  # 全屏模式不需要激活
        
        if not _HAS_QUARTZ:
            return "failed"

        # 时间节流：0.5 秒内不重复激活
        now = time.time()
        if hasattr(self, '_last_activate_time') and (now - self._last_activate_time) < 0.5:
            return "throttled"

        try:
            # 检查目标窗口是否已在最前面（Z-order 第一个可见窗口）
            window_list = CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                kCGNullWindowID,
            )
            for win in window_list:
                layer = win.get("kCGWindowLayer", 0)
                if layer != 0:
                    continue
                owner = win.get("kCGWindowOwnerName", "")
                if owner in ("Window Server", "SystemUIServer", "Dock"):
                    continue
                # 第一个有效窗口就是当前焦点窗口
                front_wid = win.get("kCGWindowNumber", 0)
                if front_wid == self._window_id:
                    # 已在最前面，不需要激活
                    return "already_front"
                break  # 不在最前面，需要激活

            from AppKit import NSWorkspace
            
            # 通过应用名找到运行中的应用并激活
            workspace = NSWorkspace.sharedWorkspace()
            running_apps = workspace.runningApplications()
            
            for app in running_apps:
                if app.localizedName() == self._window_owner:
                    # activateWithOptions_ 会将应用的窗口提升到前台
                    app.activateWithOptions_(
                        1 << 1  # NSApplicationActivateIgnoringOtherApps
                    )
                    self._last_activate_time = now
                    logger.info(f"激活窗口应用: {self._window_owner} (窗口 ID={self._window_id})")
                    return "activated"
            
            logger.warning(f"未找到运行中的应用: {self._window_owner}")
            return "failed"
        except ImportError:
            logger.warning("AppKit 不可用，无法激活窗口")
            return "failed"
        except Exception as e:
            logger.error(f"激活窗口失败: {e}")
            return "failed"

    def _capture_window(self) -> Optional[Image.Image]:
        """使用 Quartz CGWindowListCreateImage 捕获指定窗口"""
        if not _HAS_QUARTZ or self._window_id is None:
            return None

        # CGWindowListCreateImage: 截取指定窗口的画面
        cg_image = CGWindowListCreateImage(
            CGRectNull,  # CGRectNull = 自动使用窗口边界
            kCGWindowListOptionIncludingWindow,
            self._window_id,
            kCGWindowImageBoundsIgnoreFraming | kCGWindowImageNominalResolution,
        )

        if cg_image is None:
            return None

        # CGImage → PIL Image
        width = CoreGraphics.CGImageGetWidth(cg_image)
        height = CoreGraphics.CGImageGetHeight(cg_image)
        bytes_per_row = CoreGraphics.CGImageGetBytesPerRow(cg_image)

        # 获取像素数据
        data_provider = CoreGraphics.CGImageGetDataProvider(cg_image)
        pixel_data = CoreGraphics.CGDataProviderCopyData(data_provider)

        # 创建 PIL Image (BGRA → RGB)
        img = Image.frombuffer(
            "RGBA",
            (width, height),
            pixel_data,
            "raw",
            "BGRA",
            bytes_per_row,
            1,
        )
        return img.convert("RGB")

    def capture_raw(self) -> Image.Image:
        """捕获原始屏幕图像，返回 PIL Image
        
        窗口模式下截取指定窗口，全屏模式下截取整个屏幕
        """
        if self._window_id is not None:
            img = self._capture_window()
            if img is not None:
                return img
            # 窗口捕获失败（可能已关闭），自动回退全屏
            logger.warning(f"窗口 {self._window_id} 捕获失败，回退全屏模式")
            self._window_id = None
            self._window_name = None
            self._window_owner = None
            self._window_bounds = None
            # 恢复屏幕尺寸信息
            self._update_screen_info()

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
