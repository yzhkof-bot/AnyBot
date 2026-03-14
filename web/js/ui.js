/**
 * ui.js - UI 控制模块
 * 模式切换、虚拟键盘、全屏、视觉反馈、状态栏更新
 */

// ===== 模式切换 =====
function toggleMode() {
    if (state.mode === 'browse') {
        state.mode = 'control';
    } else {
        state.mode = 'browse';
        // 进入浏览模式时自动关闭键盘（浏览模式下键盘输入无效）
        if (state.keyboardOpen) {
            toggleKeyboard();
        }
    }
    updateModeUI();
    // 持久化模式状态
    if (typeof saveClientState === 'function') saveClientState();
}

function updateModeUI() {
    const btn = document.getElementById('btn-mode');
    const indicator = document.getElementById('mode-indicator');
    if (state.mode === 'browse') {
        btn.innerHTML = '👁 浏览';
        btn.style.background = '#30d158';
        btn.style.borderColor = '#30d158';
        indicator.className = 'browse';
        indicator.textContent = '浏览模式';
    } else {
        btn.innerHTML = '🖱 操作';
        btn.style.background = '#ff3b30';
        btn.style.borderColor = '#ff3b30';
        indicator.className = 'control';
        indicator.textContent = '操作模式';
    }
    // 短暂显示指示器后淡出
    indicator.style.opacity = '1';
    clearTimeout(indicator._fadeTimer);
    indicator._fadeTimer = setTimeout(() => { indicator.style.opacity = '0'; }, 1500);
}

// ===== 缩放重置 =====
function resetZoom() {
    state.view = { scale: 1, translateX: 0, translateY: 0 };
    state.canvas.style.transform = 'translate(0px, 0px) scale(1)';
    // 同步保存到窗口缩放记忆
    saveCurrentViewToCache();
}

// ===== 更多菜单 =====
function toggleMoreMenu() {
    const menu = document.getElementById('more-menu');
    const btn = document.getElementById('btn-more');
    const open = menu.classList.toggle('show');
    btn.classList.toggle('active', open);
}

function hideMoreMenu() {
    document.getElementById('more-menu').classList.remove('show');
    document.getElementById('btn-more').classList.remove('active');
}

// 点击菜单外部关闭
document.addEventListener('touchstart', (e) => {
    const menu = document.getElementById('more-menu');
    const btn = document.getElementById('btn-more');
    if (menu && menu.classList.contains('show') &&
        !menu.contains(e.target) && !btn.contains(e.target)) {
        hideMoreMenu();
    }
}, { passive: true });

// ===== 虚拟键盘 =====
function setupKeyboardInput() {
    const input = document.getElementById('text-input');
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            const text = input.value;
            if (text) {
                sendAction({ action: 'type', text: text });
                input.value = '';
            }
        }
    });
}

function toggleKeyboard() {
    state.keyboardOpen = !state.keyboardOpen;
    const panel = document.getElementById('keyboard-panel');
    const btn = document.getElementById('btn-keyboard');
    panel.classList.toggle('show', state.keyboardOpen);
    btn.classList.toggle('active', state.keyboardOpen);
    if (state.keyboardOpen) {
        document.getElementById('text-input').focus();
        // 打开键盘时自动切换到操作模式（浏览模式下键盘输入无效）
        if (state.mode === 'browse') {
            state.mode = 'control';
            updateModeUI();
            if (typeof saveClientState === 'function') saveClientState();
        }
    }
    updateCanvasSize();
}

// ===== 视觉反馈 =====
function showRipple(x, y, color) {
    const ripple = document.createElement('div');
    ripple.className = 'click-ripple';
    ripple.style.left = x + 'px';
    ripple.style.top = y + 'px';
    if (color) ripple.style.borderColor = color;
    document.body.appendChild(ripple);
    setTimeout(() => ripple.remove(), 400);
}

// ===== 状态更新 =====
function setStatus(connected, text) {
    state.connected = connected;
    document.getElementById('status-dot').className = connected ? 'connected' : '';
    document.getElementById('status-text').textContent = text;
}

function updateFps() {
    state.frameCount++;
    const now = Date.now();
    if (now - state.lastFpsTime >= 1000) {
        const elapsed = (now - state.lastFpsTime) / 1000;
        const fps = Math.round(state.frameCount / elapsed);
        document.getElementById('fps-text').textContent = `${fps} fps`;

        if (state.streamMode === 'webrtc' && state.pc) {
            // WebRTC 模式：通过 RTCPeerConnection.getStats() 获取真实流量
            state.pc.getStats().then(stats => {
                let totalBytes = 0;
                stats.forEach(report => {
                    if (report.type === 'inbound-rtp' && report.kind === 'video') {
                        totalBytes = report.bytesReceived || 0;
                    }
                });
                // 计算本周期内的增量
                const delta = totalBytes - (state._lastWebrtcBytes || 0);
                state._lastWebrtcBytes = totalBytes;
                const bytesPerSec = delta / elapsed;
                displayBandwidth(bytesPerSec);
            });
        } else {
            // MJPEG 模式：使用 WebSocket 累计的字节数
            const bytesPerSec = state.bytesReceived / elapsed;
            displayBandwidth(bytesPerSec);
            state.bytesReceived = 0;
        }

        state.frameCount = 0;
        state.lastFpsTime = now;
    }
}

function displayBandwidth(bytesPerSec) {
    let bwText;
    if (bytesPerSec >= 1048576) {
        bwText = (bytesPerSec / 1048576).toFixed(1) + ' MB/s';
    } else {
        bwText = Math.round(bytesPerSec / 1024) + ' KB/s';
    }
    document.getElementById('bw-text').textContent = bwText;
}

// ===== 全屏 + 横屏 =====
function toggleFullscreenLandscape() {
    const doc = document.documentElement;
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
        // 进入全屏
        const requestFs = doc.requestFullscreen || doc.webkitRequestFullscreen;
        if (requestFs) {
            requestFs.call(doc).then(() => {
                // 锁定横屏
                if (screen.orientation && screen.orientation.lock) {
                    screen.orientation.lock('landscape').catch(() => {});
                }
            }).catch(() => {});
        }
        document.getElementById('btn-fullscreen').classList.add('active');
        document.getElementById('btn-fullscreen').innerHTML = '🔲 退出';
    } else {
        // 退出全屏
        const exitFs = document.exitFullscreen || document.webkitExitFullscreen;
        if (exitFs) {
            exitFs.call(document).catch(() => {});
        }
        if (screen.orientation && screen.orientation.unlock) {
            screen.orientation.unlock();
        }
        document.getElementById('btn-fullscreen').classList.remove('active');
        document.getElementById('btn-fullscreen').innerHTML = '🔲 全屏';
    }
}

// 监听全屏状态变化（用户按返回键退出时同步按钮状态）
document.addEventListener('fullscreenchange', onFullscreenChange);
document.addEventListener('webkitfullscreenchange', onFullscreenChange);

function onFullscreenChange() {
    const isFs = !!(document.fullscreenElement || document.webkitFullscreenElement);
    const btn = document.getElementById('btn-fullscreen');
    btn.classList.toggle('active', isFs);
    btn.innerHTML = isFs ? '🔲 退出' : '🔲 全屏';
    if (!isFs && screen.orientation && screen.orientation.unlock) {
        screen.orientation.unlock();
    }
    setTimeout(updateCanvasSize, 100);
}

// ===== 滚轮模式开关 =====
function toggleScrollMode() {
    state.scrollMode = !state.scrollMode;
    updateScrollModeUI();
    if (typeof saveClientState === 'function') saveClientState();
}

function updateScrollModeUI() {
    const btn = document.getElementById('btn-scroll');
    btn.classList.toggle('active', state.scrollMode);

    // 滚轮模式开启时显示橙色指示器
    const indicator = document.getElementById('mode-indicator');
    if (state.scrollMode) {
        indicator.className = 'scroll';
        indicator.textContent = '滚轮模式';
    } else {
        // 恢复当前 browse/control 模式指示
        updateModeUI();
        return;
    }
    indicator.style.opacity = '1';
    clearTimeout(indicator._fadeTimer);
    indicator._fadeTimer = setTimeout(() => { indicator.style.opacity = '0'; }, 1500);
}

// ===== 窗口选择弹窗 =====

/**
 * 保存当前窗口/全屏的缩放位置到 cache，同时持久化到 localStorage
 */
function saveCurrentViewToCache() {
    const key = state.currentWindowId !== null ? state.currentWindowId : '__fullscreen__';
    state.windowViewCache[key] = { ...state.view };
    // 持久化到 localStorage
    if (typeof saveClientState === 'function') saveClientState();
}

/**
 * 从 cache 恢复指定窗口/全屏的缩放位置
 */
function restoreViewFromCache(windowId) {
    const key = windowId !== null ? windowId : '__fullscreen__';
    const cached = state.windowViewCache[key];
    if (cached) {
        state.view = { ...cached };
    } else {
        // 没有缓存，重置为默认
        state.view = { scale: 1, translateX: 0, translateY: 0 };
    }
    state.canvas.style.transform =
        `translate(${state.view.translateX}px, ${state.view.translateY}px) scale(${state.view.scale})`;
}

/**
 * 更新工具栏窗口按钮的样式
 */
function updateWindowBtnUI() {
    const btn = document.getElementById('btn-window');
    if (!btn) return;
    if (state.currentWindowId !== null) {
        btn.classList.add('active');
        btn.innerHTML = '🪟 窗口';
    } else {
        btn.classList.remove('active');
        btn.innerHTML = '🪟 窗口';
    }
}

/**
 * 显示窗口选择弹窗，从服务端获取窗口列表
 * 排序：置顶窗口 → 焦点窗口 → 其他窗口
 */
async function showWindowPicker() {
    const overlay = document.getElementById('window-picker-overlay');
    const listEl = document.getElementById('window-list');
    const loadingEl = document.getElementById('window-list-loading');
    const currentEl = document.getElementById('window-picker-current');
    const fullscreenBtn = document.getElementById('window-fullscreen-btn');

    overlay.classList.add('show');
    listEl.innerHTML = '';
    loadingEl.style.display = 'block';

    try {
        const resp = await fetch('/api/windows');
        const data = await resp.json();
        loadingEl.style.display = 'none';

        const currentWindowId = data.current_window_id;
        const pinnedIds = new Set(data.pinned_ids || []);

        // 显示当前模式
        if (currentWindowId) {
            currentEl.textContent = `当前: 窗口模式 (ID: ${currentWindowId})`;
            fullscreenBtn.classList.remove('current');
        } else {
            currentEl.textContent = '当前: 全屏模式';
            fullscreenBtn.classList.add('current');
        }

        // 渲染窗口列表
        const windows = data.windows || [];
        if (windows.length === 0) {
            listEl.innerHTML = '<div style="padding:20px;text-align:center;color:#666;">未找到可用窗口</div>';
            return;
        }

        // 分组：置顶 / 非置顶
        const pinnedWindows = windows.filter(w => w.pinned);
        const normalWindows = windows.filter(w => !w.pinned);

        // 渲染置顶分组
        if (pinnedWindows.length > 0) {
            const header = document.createElement('div');
            header.className = 'window-group-header';
            header.textContent = '📌 置顶窗口';
            listEl.appendChild(header);

            for (const win of pinnedWindows) {
                const isCurrent = (win.id === currentWindowId);
                listEl.appendChild(createWindowItem(win, { isCurrent, isPinned: true, isFocused: false }));
            }
        }

        // 渲染普通窗口分组
        if (normalWindows.length > 0) {
            if (pinnedWindows.length > 0) {
                const header = document.createElement('div');
                header.className = 'window-group-header';
                header.textContent = '🪟 所有窗口';
                listEl.appendChild(header);
            }

            for (let i = 0; i < normalWindows.length; i++) {
                const win = normalWindows[i];
                // 非置顶列表中第一个窗口（z_order 最小的）为焦点窗口
                const isFocused = (win.z_order === 0);
                const isCurrent = (win.id === currentWindowId);
                listEl.appendChild(createWindowItem(win, { isCurrent, isPinned: false, isFocused }));
            }
        }
    } catch (e) {
        loadingEl.style.display = 'none';
        listEl.innerHTML = `<div style="padding:20px;text-align:center;color:#ff3b30;">加载失败: ${e.message}</div>`;
    }
}

/**
 * 创建单个窗口列表项
 */
function createWindowItem(win, { isCurrent, isPinned, isFocused }) {
    const item = document.createElement('div');
    item.className = 'window-item' + (isCurrent ? ' current' : '') + (isPinned ? ' pinned' : '');

    // 图标
    let icon = '🪟';
    if (isPinned) icon = '📌';
    else if (isFocused) icon = '🔵';

    // 标签
    let tags = '';
    if (isFocused && !isPinned) tags += ' <span class="window-focus-tag">焦点</span>';
    if (win.offscreen) tags += ' <span class="window-offscreen-tag">超出屏幕</span>';

    item.innerHTML = `
        <div class="window-item-icon">${icon}</div>
        <div class="window-item-info">
            <div class="window-item-owner">${escapeHtml(win.owner)}${tags}</div>
            <div class="window-item-name">${escapeHtml(win.name)}</div>
        </div>
        <div class="window-item-size">${win.bounds.w}×${win.bounds.h}</div>
        <button class="window-pin-btn ${isPinned ? 'pinned' : ''}" title="${isPinned ? '取消置顶' : '置顶'}">
            ${isPinned ? '📌' : '☆'}
        </button>
    `;

    // 点击窗口项 → 选择窗口
    const infoArea = item.querySelector('.window-item-info');
    const iconArea = item.querySelector('.window-item-icon');
    const sizeArea = item.querySelector('.window-item-size');
    [infoArea, iconArea, sizeArea].forEach(el => {
        el.addEventListener('click', () => {
            selectWindow(win.id, win.name, win.owner);
        });
    });

    // 点击置顶按钮
    const pinBtn = item.querySelector('.window-pin-btn');
    pinBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (isPinned) {
            await unpinWindow(win.id);
        } else {
            await pinWindow(win.id, win.owner, win.name);
        }
        // 刷新窗口列表
        showWindowPicker();
    });

    return item;
}

/**
 * 置顶窗口
 */
async function pinWindow(windowId, owner, name) {
    try {
        await fetch('/api/window/pin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ window_id: windowId, window_owner: owner, window_name: name }),
        });
    } catch (e) {
        console.error('置顶窗口失败:', e);
    }
}

/**
 * 取消置顶窗口
 */
async function unpinWindow(windowId) {
    try {
        await fetch('/api/window/unpin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ window_id: windowId }),
        });
    } catch (e) {
        console.error('取消置顶失败:', e);
    }
}

function hideWindowPicker() {
    document.getElementById('window-picker-overlay').classList.remove('show');
}

/**
 * 选择窗口或切回全屏
 * 切换前保存当前缩放位置，切换后恢复目标窗口的缩放位置
 */
async function selectWindow(windowId, windowName, windowOwner) {
    try {
        // 1. 保存当前窗口/全屏的缩放位置
        saveCurrentViewToCache();

        // 2. 标记窗口切换过渡期（禁止操控，直到新画面到达）
        //    这解决了切换不同窗口时的坐标错乱问题：
        //    selectWindow API 返回后 screenWidth/Height 立即更新为新窗口尺寸，
        //    但 WebRTC 视频帧还在推送旧窗口画面，canvas 显示比例不一致，
        //    导致 mapToScreen 的坐标映射错乱。
        const isWindowChange = (windowId !== state.currentWindowId);
        if (isWindowChange) {
            state.windowSwitching = true;
        }

        const body = windowId !== null
            ? { window_id: windowId, window_name: windowName || '', window_owner: windowOwner || '' }
            : { window_id: null };

        const resp = await fetch('/api/window/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();

        if (data.success) {
            // 3. 更新前端的屏幕尺寸信息
            if (data.screen_info) {
                state.screenWidth = data.screen_info.width;
                state.screenHeight = data.screen_info.height;
            }

            // 4. 更新当前窗口 ID
            state.currentWindowId = windowId;

            // 5. 恢复目标窗口的缩放位置
            restoreViewFromCache(windowId);

            // 6. 更新 UI
            updateWindowBtnUI();
            hideWindowPicker();

            // 7. 显示一个模式提示
            const mode = data.mode === 'window' ? `窗口: ${windowOwner}` : '全屏模式';
            const indicator = document.getElementById('mode-indicator');
            indicator.textContent = mode;
            indicator.className = data.mode === 'window' ? 'control' : 'browse';
            indicator.style.opacity = '1';
            clearTimeout(indicator._fadeTimer);
            indicator._fadeTimer = setTimeout(() => { indicator.style.opacity = '0'; }, 2000);

            // 8. 持久化状态到 localStorage
            if (typeof saveClientState === 'function') saveClientState();

            // 9. 超时保护：如果新旧窗口尺寸相同，视频帧尺寸不会变化，
            //    render loop 不会触发解锁。设置超时自动解除切换锁定。
            if (state.windowSwitching) {
                setTimeout(() => {
                    if (state.windowSwitching) {
                        state.windowSwitching = false;
                        console.log('[AnyBot] 窗口切换超时解锁（新旧窗口尺寸可能相同）');
                    }
                }, 500);
            }
        } else {
            // API 失败，解除切换锁定
            state.windowSwitching = false;
        }
    } catch (e) {
        console.error('切换窗口失败:', e);
        state.windowSwitching = false;
    }
}

/**
 * HTML 转义
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
