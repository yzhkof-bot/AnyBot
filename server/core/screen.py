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
import os
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

# AXUIElement API（Accessibility）在 ApplicationServices / HIServices 中，不在 Quartz 中
_HAS_AX = False
if platform.system() == "Darwin":
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementPerformAction,
            AXUIElementSetAttributeValue,
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        _HAS_AX = True
        logger.info("AXUIElement (Accessibility) API 已加载")
    except ImportError as e:
        logger.warning(f"ApplicationServices 不可用，窗口焦点切换功能受限: {e}")


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
        self._pinned_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), '.anybot_pinned.json')
        self._load_pinned_windows()
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

    @property
    def physical_screen_size(self) -> Tuple[int, int]:
        """获取物理屏幕分辨率（不受窗口模式影响）
        
        screen_size 在窗口模式下会变成窗口截图的尺寸，
        但坐标裁剪、超出屏幕判断等需要用真实的屏幕物理分辨率。
        """
        monitor = self._sct.monitors[self.monitor_index]
        return (monitor["width"], monitor["height"])

    # ───────── 窗口管理 ─────────

    def list_windows(self) -> List[dict]:
        """列出所有可见窗口（供前端选择）
        
        Quartz 的 CGWindowListCopyWindowInfo 返回的窗口天然按 Z-order 排列，
        最前面的（当前焦点）窗口排在列表最前。
        
        排序规则：置顶窗口 → 焦点窗口 → 其他窗口
        
        Returns:
            [{"id": 窗口ID, "owner": 应用名, "name": 窗口标题, 
              "bounds": {x,y,w,h}, "offscreen": bool, "order": 0起始序号, "pinned": bool}, ...]
        """
        if not _HAS_QUARTZ:
            return []

        window_list = CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        )

        screen_w = self._screen_info["width"]
        screen_h = self._screen_info["height"]

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

            bx = int(bounds.get("X", 0))
            by = int(bounds.get("Y", 0))
            bw = int(w)
            bh = int(h)
            # 检测窗口是否有部分超出屏幕
            offscreen = (bx < 0 or by < 0 or bx + bw > screen_w or by + bh > screen_h)

            all_windows.append({
                "id": wid,
                "owner": owner,
                "name": name or "(无标题)",
                "bounds": {
                    "x": bx,
                    "y": by,
                    "w": bw,
                    "h": bh,
                },
                "offscreen": offscreen,
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
        self._save_pinned_windows()
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
            self._save_pinned_windows()
            logger.info(f"取消置顶窗口: ID={window_id}")
        return removed

    @property
    def pinned_window_ids(self) -> List[int]:
        """获取所有置顶的窗口 ID 列表"""
        return [p["id"] for p in self._pinned_windows]

    def _save_pinned_windows(self):
        """将置顶窗口列表持久化到 JSON 文件"""
        try:
            data = []
            for p in self._pinned_windows:
                data.append({"id": p["id"], "owner": p.get("owner", ""), "name": p.get("name", "")})
            with open(self._pinned_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存置顶窗口列表失败: {e}")

    def _load_pinned_windows(self):
        """从 JSON 文件加载置顶窗口列表"""
        try:
            if os.path.exists(self._pinned_file):
                with open(self._pinned_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._pinned_windows = data
                    logger.info(f"已加载 {len(data)} 个置顶窗口")
        except Exception as e:
            logger.warning(f"加载置顶窗口列表失败: {e}")
            self._pinned_windows = []

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
            # 切换窗口时立即激活目标窗口到前台
            # 重置节流时间，确保 activate_window 一定执行
            self._last_activate_time = 0
            activate_result = self.activate_window()
            logger.info(f"切换窗口时激活: {activate_result}")
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

    def _is_window_front(self) -> bool:
        """检查当前目标窗口是否在 Z-order 最前面"""
        if self._window_id is None:
            return True  # 全屏模式不需要检查
        if not _HAS_QUARTZ:
            return True

        try:
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
                return win.get("kCGWindowNumber", 0) == self._window_id
        except Exception:
            pass
        return False

    def activate_window(self) -> str:
        """将当前窗口模式的目标窗口激活到前台
        
        macOS 的鼠标/键盘事件会发送到屏幕绝对坐标处最前面的窗口，
        如果目标窗口被遮挡，操作会发送到错误的窗口。
        因此在执行操作前需要先将目标窗口提升到前台。
        
        实现策略（针对同应用多窗口焦点切换问题）：
        1. 先通过 NSWorkspace 激活目标应用
        2. 等待应用激活完成（关键！否则后续 AXRaise 会被覆盖）
        3. 通过 AXUIElement 执行 AXRaise 提升目标窗口
        4. 设置 AXMain=True 让目标窗口成为应用的 key window
        
        优化：
        - 检查窗口是否已在 Z-order 最前，已在最前则跳过
        - 时间节流：0.3 秒内不重复激活（避免拖拽等高频操作卡顿）
        - 先检查前台状态再检查节流，避免窗口被抢焦点后因节流跳过激活
        
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

        try:
            # 检查目标窗口是否已在最前面
            if self._is_window_front():
                return "already_front"

            # 时间节流：0.3 秒内不重复激活
            now = time.time()
            if hasattr(self, '_last_activate_time') and (now - self._last_activate_time) < 0.3:
                return "throttled"

            from AppKit import NSWorkspace, NSRunningApplication
            
            # Step 1: 激活目标应用（将应用提到前台）
            workspace = NSWorkspace.sharedWorkspace()
            running_apps = workspace.runningApplications()
            target_app = None
            target_pid = None
            
            for app in running_apps:
                if app.localizedName() == self._window_owner:
                    target_app = app
                    target_pid = app.processIdentifier()
                    app.activateWithOptions_(
                        1 << 1  # NSApplicationActivateIgnoringOtherApps
                    )
                    break
            
            if target_app is None:
                logger.warning(f"未找到运行中的应用: {self._window_owner}")
                return "failed"

            # Step 2: 等待应用激活完成
            # 这是关键！activateWithOptions_ 是异步的，如果不等就做 AXRaise，
            # macOS 完成激活后会把应用的 key window 提到前面，覆盖 AXRaise 的效果。
            # 特别是同应用多窗口场景：macOS 激活应用时会把上一个 key window 提前。
            time.sleep(0.05)  # 50ms 等待应用激活完成

            # Step 3: 通过 AXUIElement 精确激活指定窗口
            # AXRaise + AXMain 双管齐下，解决同应用多窗口焦点切换问题
            raised = self._raise_window_by_ax(target_pid)
            
            self._last_activate_time = time.time()  # 用实际时间（含 sleep）
            if raised:
                logger.info(f"激活窗口: [{self._window_owner}] {self._window_name} "
                           f"(ID={self._window_id}, AXRaise+AXMain 成功)")
            else:
                logger.info(f"激活应用: {self._window_owner} (AXRaise 未匹配，回退应用级激活)")
            return "activated"

        except ImportError:
            logger.warning("AppKit 不可用，无法激活窗口")
            return "failed"
        except Exception as e:
            logger.error(f"激活窗口失败: {e}")
            return "failed"

    def _raise_window_by_ax(self, pid: int) -> bool:
        """通过 Accessibility API (AXUIElement) 精确提升指定窗口到前台
        
        遍历目标应用的所有 AX 窗口，通过对比窗口位置/尺寸来匹配
        CGWindowID 对应的窗口，然后执行：
        1. AXRaise —— 将窗口提升到 Z-order 最前
        2. 设置 AXMain=True —— 将窗口设为应用的 main window（key window）
        3. 设置 AXFocused=True —— 确保窗口获得键盘焦点
        
        这三步缺一不可：
        - 只做 AXRaise：窗口到了前面但不是 key window，macOS 可能重新调整
        - 只设 AXMain：窗口成为 key window 但 Z-order 可能不对
        - 三者结合：确保窗口既在最前面又是 key window
        
        匹配策略（针对同应用多窗口场景，如多个终端窗口）：
        1. 优先「位置+尺寸」精确匹配（最可靠，因为同应用多窗口标题可能完全相同）
        2. 如果位置匹配失败，才回退到「标题+尺寸」匹配
        
        Args:
            pid: 目标应用的进程 ID
            
        Returns:
            True 表示成功 raise 了目标窗口，False 表示未找到匹配窗口
        """
        if not _HAS_AX:
            logger.warning("AXUIElement API 不可用，无法精确切换窗口焦点")
            return False

        try:
            # 获取目标窗口的 CGWindow 信息（标题 + bounds）用于匹配
            target_info = None
            window_list = CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                kCGNullWindowID,
            )
            for win in window_list:
                if win.get("kCGWindowNumber", 0) == self._window_id:
                    bounds = win.get("kCGWindowBounds", {})
                    target_info = {
                        "name": win.get("kCGWindowName", ""),
                        "x": int(bounds.get("X", 0)),
                        "y": int(bounds.get("Y", 0)),
                        "w": int(bounds.get("Width", 0)),
                        "h": int(bounds.get("Height", 0)),
                    }
                    break
            
            if target_info is None:
                logger.warning(f"AXRaise: CGWindow 中未找到窗口 ID={self._window_id}")
                return False

            logger.debug(f"AXRaise 目标: ID={self._window_id} title='{target_info['name']}' "
                        f"pos=({target_info['x']},{target_info['y']}) "
                        f"size=({target_info['w']}x{target_info['h']})")

            # 创建应用级 AXUIElement（使用 ApplicationServices 的 API）
            app_ref = AXUIElementCreateApplication(pid)
            
            # 获取应用的所有窗口
            err, ax_windows = AXUIElementCopyAttributeValue(
                app_ref, "AXWindows", None
            )
            if err != 0 or ax_windows is None:
                logger.warning(f"AXRaise: 无法获取应用窗口列表, AX err={err}")
                return False

            logger.debug(f"AXRaise: 应用 PID={pid} 共有 {len(ax_windows)} 个 AX 窗口")

            # 收集所有 AX 窗口信息，用于匹配和日志
            ax_win_infos = []
            for ax_win in ax_windows:
                # 获取 AX 窗口标题
                err, ax_title = AXUIElementCopyAttributeValue(
                    ax_win, "AXTitle", None
                )
                ax_title = ax_title if err == 0 and ax_title else ""

                # 获取 AX 窗口位置
                err, ax_pos = AXUIElementCopyAttributeValue(
                    ax_win, "AXPosition", None
                )
                # 获取 AX 窗口尺寸
                err2, ax_size = AXUIElementCopyAttributeValue(
                    ax_win, "AXSize", None
                )

                ax_x = ax_y = ax_w = ax_h = 0
                if err == 0 and ax_pos is not None:
                    pos_val = AXValueGetValue(ax_pos, kAXValueCGPointType, None)
                    if pos_val:
                        ax_x, ax_y = int(pos_val[1].x), int(pos_val[1].y)
                if err2 == 0 and ax_size is not None:
                    size_val = AXValueGetValue(ax_size, kAXValueCGSizeType, None)
                    if size_val:
                        ax_w, ax_h = int(size_val[1].width), int(size_val[1].height)

                ax_win_infos.append({
                    "ref": ax_win,
                    "title": ax_title,
                    "x": ax_x, "y": ax_y,
                    "w": ax_w, "h": ax_h,
                })
                logger.debug(f"  AX窗口: title='{ax_title}' pos=({ax_x},{ax_y}) size=({ax_w}x{ax_h})")

            # 匹配并激活目标窗口
            matched_win = self._match_ax_window(ax_win_infos, target_info)
            if matched_win is not None:
                self._activate_ax_window(matched_win, app_ref)
                return True

            logger.warning(f"AXRaise: 所有匹配策略均未命中")
            return False
        except Exception as e:
            logger.error(f"AXRaise 异常: {e}")
            return False

    def _match_ax_window(self, ax_win_infos: list, target_info: dict) -> Optional[dict]:
        """从 AX 窗口列表中匹配目标窗口
        
        匹配策略优先级（核心原则：标题优先，位置辅助）：
        1. 标题+位置+尺寸 全匹配（最可靠，唯一标识窗口）
        2. 标题+尺寸 匹配（窗口可能被拖动过，位置变化）
        3. 仅标题 匹配（窗口可能被缩放过）
        4. 位置+尺寸 匹配（窗口标题为空或 AX 标题与 CG 标题不一致时回退）
        
        注意：不能把「位置+尺寸」放在最高优先级！
        因为同一应用的多个最大化窗口拥有完全相同的位置和尺寸，
        此时「位置+尺寸」会总是命中 AX 列表中的第一个窗口（通常是错误的那个）。
        标题才是区分同应用不同窗口的关键字段。
        """
        TOLERANCE = 10  # 像素容差

        # Pass 1: 标题+位置+尺寸 全匹配（最精确）
        if target_info["name"]:
            for info in ax_win_infos:
                if (info["title"] == target_info["name"] and
                    abs(info["x"] - target_info["x"]) <= TOLERANCE and
                    abs(info["y"] - target_info["y"]) <= TOLERANCE and
                    abs(info["w"] - target_info["w"]) <= TOLERANCE and
                    abs(info["h"] - target_info["h"]) <= TOLERANCE):
                    logger.debug(f"AX匹配(标题+位置+尺寸): title='{info['title']}' pos=({info['x']},{info['y']})")
                    return info

        # Pass 2: 标题+尺寸匹配（位置可能有变化）
        if target_info["name"]:
            for info in ax_win_infos:
                if (info["title"] == target_info["name"] and
                    abs(info["w"] - target_info["w"]) <= TOLERANCE and
                    abs(info["h"] - target_info["h"]) <= TOLERANCE):
                    logger.debug(f"AX匹配(标题+尺寸): title='{info['title']}'")
                    return info

        # Pass 3: 仅标题匹配（窗口可能被缩放过）
        if target_info["name"]:
            for info in ax_win_infos:
                if info["title"] == target_info["name"]:
                    logger.debug(f"AX匹配(仅标题): title='{info['title']}'")
                    return info

        # Pass 4: 位置+尺寸匹配（回退方案，标题为空或 AX/CG 标题不一致时）
        # 注意：同应用多个最大化窗口可能位置尺寸完全相同，此策略可能误匹配
        for info in ax_win_infos:
            if (abs(info["x"] - target_info["x"]) <= TOLERANCE and
                abs(info["y"] - target_info["y"]) <= TOLERANCE and
                abs(info["w"] - target_info["w"]) <= TOLERANCE and
                abs(info["h"] - target_info["h"]) <= TOLERANCE):
                logger.debug(f"AX匹配(位置+尺寸,回退): title='{info['title']}' pos=({info['x']},{info['y']})")
                return info

        return None

    def _activate_ax_window(self, win_info: dict, app_ref) -> None:
        """对匹配到的 AX 窗口执行完整的激活操作
        
        三步激活确保窗口获得焦点：
        1. AXRaise — 提升 Z-order
        2. AXMain=True — 设为应用的 main window
        3. AXFocused=True — 设为应用的 focused window
        """
        ax_win = win_info["ref"]

        # Step 1: AXRaise — 提升到 Z-order 最前
        err = AXUIElementPerformAction(ax_win, "AXRaise")
        logger.debug(f"AXRaise: err={err}")

        # Step 2: 设置为 main window（key window）
        # 这是关键！没有这一步，macOS 可能在激活动画完成后把原来的 key window 拉回前台
        try:
            from CoreFoundation import kCFBooleanTrue
            err2 = AXUIElementSetAttributeValue(ax_win, "AXMain", kCFBooleanTrue)
            logger.debug(f"AXMain=True: err={err2}")
        except Exception as e:
            logger.debug(f"设置 AXMain 失败（可忽略）: {e}")

        # Step 3: 设置 focused window
        try:
            from CoreFoundation import kCFBooleanTrue
            # 设置应用级别的 AXFocusedWindow 属性指向目标窗口
            err3 = AXUIElementSetAttributeValue(app_ref, "AXFocusedWindow", ax_win)
            logger.debug(f"AXFocusedWindow: err={err3}")
        except Exception as e:
            logger.debug(f"设置 AXFocusedWindow 失败（可忽略）: {e}")

        logger.info(f"窗口激活完成: title='{win_info['title']}' "
                   f"pos=({win_info['x']},{win_info['y']})")

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
