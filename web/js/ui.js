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
    }
    updateModeUI();
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
    document.getElementById('btn-zoom').textContent = '🔍 1x';
}

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
        const fps = state.frameCount;
        document.getElementById('fps-text').textContent = `${fps} fps`;
        state.frameCount = 0;
        state.lastFpsTime = now;
    }
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
