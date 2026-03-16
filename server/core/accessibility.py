"""
macOS Accessibility API 控件树获取模块

通过 pyobjc 的 AXUIElement API 递归遍历 UI 元素树，
提取每个元素的角色、标题、描述、位置、尺寸等结构化信息。
输出为缩进文本格式（类似 XcodeBuildMCP 的 snapshot_ui），节省 token。

全屏模式：获取前台应用 + 系统菜单栏 + Dock 栏的完整控件树，
覆盖屏幕上所有可交互区域。

坐标系统：AXPosition 返回的是屏幕物理绝对坐标，
与 Agent 使用的 execute_absolute() 坐标系统完全一致，无需转换。

依赖：pyobjc-framework-ApplicationServices（已在 requirements.txt 中）
"""

import platform
from typing import Optional, List, Set

from loguru import logger

# AXUIElement API（Accessibility）
_HAS_AX = False
_HAS_QUARTZ = False
if platform.system() == "Darwin":
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCreateSystemWide,
            AXUIElementCopyAttributeValue,
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        _HAS_AX = True
    except ImportError as e:
        logger.warning(f"ApplicationServices 不可用，Accessibility 控件树功能不可用: {e}")
    
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            kCGWindowOwnerPID,
            kCGWindowLayer,
            kCGWindowBounds,
        )
        _HAS_QUARTZ = True
    except ImportError as e:
        logger.warning(f"Quartz 不可用，无法获取可见窗口列表: {e}")


class AccessibilityInspector:
    """macOS Accessibility API 控件树获取器
    
    通过 AXUIElement API 递归遍历 UI 元素层次结构，
    提取角色/标题/坐标/尺寸等结构化信息，输出为缩进文本格式。
    
    全屏模式下会获取：
    1. 当前前台应用的完整控件树
    2. 系统菜单栏（顶部 Menu Bar）
    3. Dock 栏（底部/侧边）
    
    输出示例:
        === 前台应用 ===
        App: "Safari" (PID: 1234)
        [AXWindow] "百度一下" (0, 25, 1440, 875)
          [AXToolbar] (0, 25, 1440, 52)
            [AXButton] "返回" (12, 33, 36, 36)

        === 系统菜单栏 ===
        App: "SystemUIServer" (PID: 567)
          [AXMenuBar] (0, 0, 1440, 25)
            [AXMenuBarItem] "Apple" (0, 0, 30, 25)

        === Dock 栏 ===
        App: "Dock" (PID: 890)
          [AXList] "Dock" (200, 1050, 1000, 70)
            [AXDockItem] "Finder" (210, 1055, 60, 60)
    """
    
    # 最大递归深度（防止超深嵌套导致性能问题）
    MAX_DEPTH: int = 10
    # 每个部分（前台应用/菜单栏/Dock）各自的最大元素数量
    MAX_ELEMENTS_PER_SECTION: int = 500
    # 每层缩进空格数
    INDENT_SIZE: int = 2
    
    # 系统进程名称（用于查找 PID）
    _DOCK_BUNDLE_ID = "com.apple.dock"
    _SYSTEM_UI_BUNDLE_ID = "com.apple.systemuiserver"
    
    def get_tree(self, pid: Optional[int] = None, fullscreen: bool = True) -> str:
        """获取 UI 控件树（缩进文本格式）
        
        Args:
            pid: 目标应用的进程 ID，None 则自动获取当前前台应用
            fullscreen: 是否获取全屏控件树（包含所有可见应用 + 菜单栏 + Dock 栏）
            
        Returns:
            缩进文本格式的控件树字符串，
            如果失败返回错误描述字符串
        """
        if not _HAS_AX:
            return "[Error] macOS Accessibility API 不可用（需要 pyobjc-framework-ApplicationServices）"
        
        # 获取前台应用 PID
        frontmost_pid = self._get_frontmost_pid()
        if pid is None:
            pid = frontmost_pid
            if pid is None:
                return "[Error] 无法获取当前前台应用的 PID"
        
        sections = []
        
        if fullscreen:
            # === 全屏模式：获取所有可见应用的控件树 ===
            
            # 1. 前台应用控件树（最重要，放在最前面）
            app_tree = self._get_app_tree(pid)
            sections.append(f"=== 前台应用 ===\n{app_tree}")
            
            # 2. 其他可见应用的控件树
            other_trees = self._get_other_visible_apps_tree(pid)
            if other_trees:
                sections.append(f"=== 其他可见应用 ===\n{other_trees}")
            
            # 3. 系统菜单栏（顶部 Menu Bar）
            menu_bar_tree = self._get_menu_bar_tree(pid)
            if menu_bar_tree:
                sections.append(f"=== 系统菜单栏 ===\n{menu_bar_tree}")
            
            # 4. Dock 栏
            dock_tree = self._get_dock_tree()
            if dock_tree:
                sections.append(f"=== Dock 栏 ===\n{dock_tree}")
        else:
            # 非全屏模式：仅获取指定应用
            app_tree = self._get_app_tree(pid)
            sections.append(app_tree)
        
        return "\n\n".join(sections)
    
    def _get_app_tree(self, pid: int) -> str:
        """获取指定应用的控件树"""
        app_name = self._get_app_name(pid)
        
        try:
            app_ref = AXUIElementCreateApplication(pid)
            lines = []
            count = [0]
            
            lines.append(f'App: "{app_name}" (PID: {pid})')
            
            # 从应用的窗口开始遍历
            err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
            if err == 0 and windows:
                for window in windows:
                    self._walk_element(window, depth=0, lines=lines, count=count,
                                       max_elements=self.MAX_ELEMENTS_PER_SECTION)
            else:
                err, children = AXUIElementCopyAttributeValue(app_ref, "AXChildren", None)
                if err == 0 and children:
                    for child in children:
                        self._walk_element(child, depth=0, lines=lines, count=count,
                                           max_elements=self.MAX_ELEMENTS_PER_SECTION)
                else:
                    lines.append(f"  [Warning] 无法获取应用的窗口或子元素 (AX err={err})")
            
            if count[0] >= self.MAX_ELEMENTS_PER_SECTION:
                lines.append(f"\n[Truncated] 元素数量已达上限 ({self.MAX_ELEMENTS_PER_SECTION})，剩余元素已省略")
            
            result = "\n".join(lines)
            logger.debug(f"[Accessibility] 前台应用控件树: PID={pid}, 元素数={count[0]}")
            return result
            
        except Exception as e:
            error_msg = f'App: "{app_name}" (PID: {pid})\n  [Error] 获取控件树失败: {e}'
            logger.error(f"[Accessibility] {error_msg}")
            return error_msg
    
    def _get_visible_app_pids(self, exclude_pids: Optional[Set[int]] = None) -> List[int]:
        """获取所有在屏幕上有可见窗口的应用 PID 列表
        
        通过 CGWindowListCopyWindowInfo 获取当前屏幕上的所有可见窗口，
        提取去重后的 PID 列表。
        
        Args:
            exclude_pids: 需要排除的 PID 集合（如前台应用、Dock、SystemUIServer）
            
        Returns:
            去重后的 PID 列表（按窗口 z-order 排列，最前面的在前）
        """
        if not _HAS_QUARTZ:
            return []
        
        if exclude_pids is None:
            exclude_pids = set()
        
        try:
            # 获取屏幕上所有可见窗口（kCGWindowListOptionOnScreenOnly）
            window_list = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly, kCGNullWindowID
            )
            
            if not window_list:
                return []
            
            seen_pids: Set[int] = set()
            ordered_pids: List[int] = []
            
            for win_info in window_list:
                win_pid = win_info.get(kCGWindowOwnerPID, 0)
                win_layer = win_info.get(kCGWindowLayer, 0)
                win_bounds = win_info.get(kCGWindowBounds, {})
                
                # 跳过已排除的 PID
                if win_pid in exclude_pids or win_pid in seen_pids:
                    continue
                
                # 只关注普通窗口层（layer == 0），跳过菜单栏/Dock/桌面等系统层
                if win_layer != 0:
                    continue
                
                # 跳过极小的窗口（可能是隐藏/不可见窗口）
                w = win_bounds.get('Width', 0)
                h = win_bounds.get('Height', 0)
                if w < 50 or h < 50:
                    continue
                
                seen_pids.add(win_pid)
                ordered_pids.append(win_pid)
            
            logger.debug(f"[Accessibility] 可见窗口应用 PID: {ordered_pids} (排除: {exclude_pids})")
            return ordered_pids
            
        except Exception as e:
            logger.debug(f"[Accessibility] 获取可见窗口列表失败: {e}")
            return []
    
    def _get_other_visible_apps_tree(self, frontmost_pid: int) -> Optional[str]:
        """获取除前台应用外的所有可见应用的控件树
        
        Args:
            frontmost_pid: 前台应用的 PID（已在 get_tree 中处理）
            
        Returns:
            所有其他可见应用的控件树合并文本，或 None
        """
        # 排除前台应用、Dock、SystemUIServer（这些在其他 section 中已获取）
        exclude_pids: Set[int] = {frontmost_pid}
        
        dock_pid = self._get_pid_by_bundle_id(self._DOCK_BUNDLE_ID)
        if dock_pid:
            exclude_pids.add(dock_pid)
        
        sys_ui_pid = self._get_pid_by_bundle_id(self._SYSTEM_UI_BUNDLE_ID)
        if sys_ui_pid:
            exclude_pids.add(sys_ui_pid)
        
        visible_pids = self._get_visible_app_pids(exclude_pids=exclude_pids)
        
        if not visible_pids:
            return None
        
        # 限制最多获取的应用数量（防止窗口太多导致 token 爆炸）
        MAX_OTHER_APPS = 5
        if len(visible_pids) > MAX_OTHER_APPS:
            visible_pids = visible_pids[:MAX_OTHER_APPS]
            logger.debug(f"[Accessibility] 其他可见应用过多，只获取前 {MAX_OTHER_APPS} 个")
        
        app_sections = []
        total_elements = 0
        
        for app_pid in visible_pids:
            app_name = self._get_app_name(app_pid)
            
            try:
                app_ref = AXUIElementCreateApplication(app_pid)
                lines = []
                count = [0]
                
                lines.append(f'App: "{app_name}" (PID: {app_pid})')
                
                # 获取窗口
                err, windows = AXUIElementCopyAttributeValue(app_ref, "AXWindows", None)
                if err == 0 and windows:
                    for window in windows:
                        # 每个非前台应用限制较少的元素数量（节省 token）
                        self._walk_element(window, depth=0, lines=lines, count=count,
                                           max_elements=200)
                else:
                    err, children = AXUIElementCopyAttributeValue(app_ref, "AXChildren", None)
                    if err == 0 and children:
                        for child in children:
                            self._walk_element(child, depth=0, lines=lines, count=count,
                                               max_elements=200)
                
                if count[0] > 0:  # 只添加有元素的应用
                    app_sections.append("\n".join(lines))
                    total_elements += count[0]
                    
            except Exception as e:
                logger.debug(f"[Accessibility] 获取应用 {app_name}(PID={app_pid}) 控件树失败: {e}")
        
        if not app_sections:
            return None
        
        result = "\n\n".join(app_sections)
        logger.debug(f"[Accessibility] 其他可见应用控件树: {len(app_sections)} 个应用, 总元素数={total_elements}")
        return result
    
    def _get_menu_bar_tree(self, frontmost_pid: int) -> Optional[str]:
        """获取系统菜单栏控件树
        
        macOS 菜单栏由两部分组成：
        1. 前台应用的菜单栏（左侧：如 "File", "Edit" 等）→ 属于前台应用的 AXMenuBar
        2. 系统状态栏（右侧：如 WiFi、电池、时钟等）→ 属于 SystemUIServer
        
        这里获取前台应用的菜单栏（AXMenuBar 属性），
        以及 SystemUIServer 的状态栏。
        """
        lines = []
        count = [0]
        
        try:
            # (a) 前台应用的菜单栏
            app_ref = AXUIElementCreateApplication(frontmost_pid)
            err, menu_bar = AXUIElementCopyAttributeValue(app_ref, "AXMenuBar", None)
            if err == 0 and menu_bar:
                app_name = self._get_app_name(frontmost_pid)
                lines.append(f'MenuBar: "{app_name}" (应用菜单)')
                self._walk_element(menu_bar, depth=0, lines=lines, count=count,
                                   max_elements=200)  # 菜单栏元素不会太多
        except Exception as e:
            logger.debug(f"[Accessibility] 获取应用菜单栏失败: {e}")
        
        try:
            # (b) SystemUIServer 的状态栏（右侧的 WiFi/电池/时钟等）
            sys_ui_pid = self._get_pid_by_bundle_id(self._SYSTEM_UI_BUNDLE_ID)
            if sys_ui_pid:
                sys_ref = AXUIElementCreateApplication(sys_ui_pid)
                err, children = AXUIElementCopyAttributeValue(sys_ref, "AXChildren", None)
                if err == 0 and children:
                    if lines:
                        lines.append("")  # 分隔前台应用菜单和系统状态栏
                    lines.append('StatusBar: "SystemUIServer" (系统状态栏)')
                    for child in children:
                        self._walk_element(child, depth=0, lines=lines, count=count,
                                           max_elements=200)
        except Exception as e:
            logger.debug(f"[Accessibility] 获取系统状态栏失败: {e}")
        
        if not lines:
            return None
        
        result = "\n".join(lines)
        logger.debug(f"[Accessibility] 菜单栏控件树: 元素数={count[0]}")
        return result
    
    def _get_dock_tree(self) -> Optional[str]:
        """获取 Dock 栏控件树"""
        try:
            dock_pid = self._get_pid_by_bundle_id(self._DOCK_BUNDLE_ID)
            if not dock_pid:
                logger.debug("[Accessibility] 未找到 Dock 进程")
                return None
            
            dock_ref = AXUIElementCreateApplication(dock_pid)
            lines = []
            count = [0]
            
            lines.append(f'Dock: (PID: {dock_pid})')
            
            # Dock 的子元素通常是 AXList（包含 Dock 上的应用图标）
            err, children = AXUIElementCopyAttributeValue(dock_ref, "AXChildren", None)
            if err == 0 and children:
                for child in children:
                    self._walk_element(child, depth=0, lines=lines, count=count,
                                       max_elements=200)  # Dock 元素不会太多
            else:
                lines.append(f"  [Warning] 无法获取 Dock 子元素 (AX err={err})")
            
            result = "\n".join(lines)
            logger.debug(f"[Accessibility] Dock 控件树: 元素数={count[0]}")
            return result
            
        except Exception as e:
            logger.debug(f"[Accessibility] 获取 Dock 控件树失败: {e}")
            return None
    
    def _get_frontmost_pid(self) -> Optional[int]:
        """获取当前前台应用的 PID"""
        try:
            from AppKit import NSWorkspace
            workspace = NSWorkspace.sharedWorkspace()
            front_app = workspace.frontmostApplication()
            if front_app:
                pid = front_app.processIdentifier()
                app_name = front_app.localizedName() or "Unknown"
                logger.debug(f"[Accessibility] 前台应用: {app_name} (PID: {pid})")
                return pid
            return None
        except Exception as e:
            logger.error(f"[Accessibility] 获取前台应用失败: {e}")
            return None
    
    def _get_app_name(self, pid: int) -> str:
        """根据 PID 获取应用名称"""
        try:
            from AppKit import NSWorkspace
            workspace = NSWorkspace.sharedWorkspace()
            for app in workspace.runningApplications():
                if app.processIdentifier() == pid:
                    return app.localizedName() or "Unknown"
            return "Unknown"
        except Exception:
            return "Unknown"
    
    def _get_pid_by_bundle_id(self, bundle_id: str) -> Optional[int]:
        """根据 Bundle ID 获取进程 PID"""
        try:
            from AppKit import NSWorkspace
            workspace = NSWorkspace.sharedWorkspace()
            for app in workspace.runningApplications():
                if app.bundleIdentifier() == bundle_id:
                    return app.processIdentifier()
            return None
        except Exception:
            return None
    
    def _walk_element(self, element, depth: int, lines: list, count: list,
                     max_elements: Optional[int] = None) -> None:
        """递归遍历 AXUIElement 子树
        
        Args:
            element: AXUIElement 引用
            depth: 当前递归深度
            lines: 输出行列表（就地追加）
            count: [当前元素计数]（用 list 包装以便修改）
            max_elements: 最大元素数量限制（None 则使用 MAX_ELEMENTS_PER_SECTION）
        """
        if max_elements is None:
            max_elements = self.MAX_ELEMENTS_PER_SECTION
        
        # 检查深度和数量限制
        if depth > self.MAX_DEPTH:
            return
        if count[0] >= max_elements:
            return
        
        # 提取元素属性
        role = self._get_attr(element, "AXRole") or ""
        if not role:
            # 没有 role 的元素跳过
            return
        
        # 获取标签文本（按优先级：AXTitle > AXDescription > AXValue）
        label = (
            self._get_attr(element, "AXTitle")
            or self._get_attr(element, "AXDescription")
            or ""
        )
        
        # 对于文本类元素，也尝试获取 AXValue
        value = ""
        if role in ("AXTextField", "AXTextArea", "AXStaticText", "AXComboBox"):
            value = self._get_attr(element, "AXValue") or ""
            if value and not label:
                label = value
            elif value and value != label:
                # 同时有 label 和 value，合并显示
                label = f"{label} | {value}"
        
        # 获取位置和尺寸
        x, y = self._get_position(element)
        w, h = self._get_size(element)
        
        # 格式化并添加到输出
        indent = " " * (self.INDENT_SIZE * (depth + 1))  # +1 因为第 0 层是 App 信息
        line = self._format_element(role, label, x, y, w, h)
        lines.append(f"{indent}{line}")
        count[0] += 1
        
        # 递归遍历子元素
        err, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
        if err == 0 and children:
            for child in children:
                self._walk_element(child, depth + 1, lines, count, max_elements)
    
    def _format_element(self, role: str, label: str, x: int, y: int, w: int, h: int) -> str:
        """格式化单个元素为文本行
        
        格式：[AXRole] "label" (x, y, w, h)
        如果没有 label 则省略引号部分
        如果没有有效坐标则省略坐标部分
        """
        parts = [f"[{role}]"]
        
        if label:
            # 截断过长的 label（防止单个元素占用太多 token）
            if len(label) > 80:
                label = label[:77] + "..."
            parts.append(f'"{label}"')
        
        if x is not None and y is not None and w is not None and h is not None:
            parts.append(f"({x}, {y}, {w}, {h})")
        
        return " ".join(parts)
    
    def _get_attr(self, element, attr_name: str) -> Optional[str]:
        """安全获取 AXUIElement 的字符串属性"""
        try:
            err, value = AXUIElementCopyAttributeValue(element, attr_name, None)
            if err == 0 and value is not None:
                # 转为字符串
                s = str(value).strip()
                return s if s else None
            return None
        except Exception:
            return None
    
    def _get_position(self, element) -> tuple:
        """获取元素的屏幕坐标 (x, y)"""
        try:
            err, ax_pos = AXUIElementCopyAttributeValue(element, "AXPosition", None)
            if err == 0 and ax_pos is not None:
                pos_val = AXValueGetValue(ax_pos, kAXValueCGPointType, None)
                if pos_val:
                    return int(pos_val[1].x), int(pos_val[1].y)
            return None, None
        except Exception:
            return None, None
    
    def _get_size(self, element) -> tuple:
        """获取元素的尺寸 (width, height)"""
        try:
            err, ax_size = AXUIElementCopyAttributeValue(element, "AXSize", None)
            if err == 0 and ax_size is not None:
                size_val = AXValueGetValue(ax_size, kAXValueCGSizeType, None)
                if size_val:
                    return int(size_val[1].width), int(size_val[1].height)
            return None, None
        except Exception:
            return None, None


# 模块级单例
_inspector = AccessibilityInspector()


def get_accessibility_tree(pid: Optional[int] = None) -> str:
    """模块级便捷函数：获取控件树
    
    Args:
        pid: 目标应用 PID，None 则自动获取前台应用
        
    Returns:
        缩进文本格式的控件树
    """
    return _inspector.get_tree(pid)
