/**
 * app.js - 核心应用模块
 * 全局状态、初始化、WebSocket 连接、画面适配、坐标映射
 * 支持增量传输：完整帧 / dirty region / skip
 */

// ===== 全局状态 =====
const state = {
    screenWs: null,
    controlWs: null,
    connected: false,
    canvas: null,
    ctx: null,
    screenWidth: 0,    // Mac 屏幕实际宽度
    screenHeight: 0,   // Mac 屏幕实际高度
    canvasDisplayW: 0,  // canvas 在页面上的显示宽度
    canvasDisplayH: 0,  // canvas 在页面上的显示高度
    imageWidth: 0,      // 接收到的图片宽度
    imageHeight: 0,     // 接收到的图片高度
    frameCount: 0,
    lastFpsTime: 0,
    keyboardOpen: false,
    // 模式: 'browse' | 'control'
    mode: 'browse',
    // 浏览模式的变换状态
    view: { scale: 1, translateX: 0, translateY: 0 },
    // 增量传输统计
    delta: {
        fullFrames: 0,
        deltaFrames: 0,
        skipFrames: 0,
    },
};

// ===== 初始化 =====
function init() {
    state.canvas = document.getElementById('screen-canvas');
    state.ctx = state.canvas.getContext('2d');
    setupTouchEvents();
    setupKeyboardInput();
}

// ===== 连接 =====
async function startConnection() {
    const btn = document.getElementById('connect-btn');
    const info = document.getElementById('connect-info');
    btn.disabled = true;
    btn.textContent = '连接中...';

    try {
        // 先获取屏幕信息
        const host = location.host || 'localhost:8765';
        const protocol = location.protocol === 'https:' ? 'https' : 'http';
        const resp = await fetch(`${protocol}://${host}/api/screen/info`);
        const screenInfo = await resp.json();
        state.screenWidth = screenInfo.width;
        state.screenHeight = screenInfo.height;

        // 建立 WebSocket
        const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
        connectScreenWs(`${wsProtocol}://${host}/ws/screen`);
        connectControlWs(`${wsProtocol}://${host}/ws/control`);

        document.getElementById('connect-overlay').classList.add('hidden');
        setStatus(true, '已连接');
    } catch (e) {
        info.textContent = `连接失败: ${e.message}`;
        btn.disabled = false;
        btn.textContent = '重新连接';
    }
}

function connectScreenWs(url) {
    state.screenWs = new WebSocket(url);
    state.screenWs.binaryType = 'blob';

    state.screenWs.onmessage = (event) => {
        if (typeof event.data === 'string') {
            // 文本消息：cursor / delta / skip
            handleTextMessage(event.data);
        } else {
            // 二进制消息：完整 JPEG 帧
            handleFullFrame(event.data);
        }
    };

    state.screenWs.onclose = () => {
        setStatus(false, '画面连接断开');
        setTimeout(() => { if (state.connected) connectScreenWs(url); }, 2000);
    };
}

/**
 * 处理完整帧（二进制 JPEG）
 */
function handleFullFrame(blob) {
    state.delta.fullFrames++;
    const img = new Image();
    const objectUrl = URL.createObjectURL(blob);
    img.onload = () => {
        // 首次或尺寸变化时更新 canvas
        if (state.canvas.width !== img.width || state.canvas.height !== img.height) {
            state.canvas.width = img.width;
            state.canvas.height = img.height;
            state.imageWidth = img.width;
            state.imageHeight = img.height;
            updateCanvasSize();
        }
        state.ctx.drawImage(img, 0, 0);
        URL.revokeObjectURL(objectUrl);
        updateFps();
    };
    img.src = objectUrl;
}

/**
 * 处理文本消息（cursor / delta / skip）
 */
function handleTextMessage(text) {
    try {
        const msg = JSON.parse(text);

        switch (msg.type) {
            case 'cursor':
                handleCursorMessage(msg);
                break;

            case 'delta':
                handleDeltaFrame(msg);
                break;

            case 'skip':
                state.delta.skipFrames++;
                // 无变化，不更新画面，但计入 fps
                updateFps();
                break;

            default:
                // 未知消息类型，忽略
                break;
        }
    } catch (e) {
        // JSON 解析失败，忽略
    }
}

/**
 * 处理光标位置消息
 */
function handleCursorMessage(msg) {
    // 更新光标指示器位置（如有需要可在 ui.js 实现）
    if (typeof updateCursorIndicator === 'function') {
        updateCursorIndicator(msg.x, msg.y);
    }

    // 更新 fps 显示
    if (msg.fps !== undefined) {
        const fpsEl = document.getElementById('fps-text');
        if (fpsEl) fpsEl.dataset.serverFps = msg.fps;
    }
}

/**
 * 处理增量帧（dirty regions）
 * 每个 region 是一个小的 JPEG 图片，绘制到 canvas 的指定位置
 */
function handleDeltaFrame(msg) {
    state.delta.deltaFrames++;

    if (!msg.regions || msg.regions.length === 0) {
        updateFps();
        return;
    }

    // 并行加载所有 region 的 JPEG
    let loadedCount = 0;
    const total = msg.regions.length;

    msg.regions.forEach((region) => {
        const img = new Image();
        img.onload = () => {
            // 绘制到 canvas 的指定位置
            state.ctx.drawImage(img, region.x, region.y, region.w, region.h);
            loadedCount++;
            if (loadedCount === total) {
                updateFps();
            }
        };
        img.onerror = () => {
            loadedCount++;
            if (loadedCount === total) {
                updateFps();
            }
        };
        img.src = 'data:image/jpeg;base64,' + region.data;
    });
}

function connectControlWs(url) {
    state.controlWs = new WebSocket(url);

    state.controlWs.onclose = () => {
        setTimeout(() => { if (state.connected) connectControlWs(url); }, 2000);
    };
}

// ===== 画面适配 =====
function updateCanvasSize() {
    const container = document.getElementById('screen-container');
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const imgRatio = state.imageWidth / state.imageHeight;
    const containerRatio = cw / ch;

    if (imgRatio > containerRatio) {
        state.canvasDisplayW = cw;
        state.canvasDisplayH = cw / imgRatio;
    } else {
        state.canvasDisplayH = ch;
        state.canvasDisplayW = ch * imgRatio;
    }

    state.canvas.style.width = state.canvasDisplayW + 'px';
    state.canvas.style.height = state.canvasDisplayH + 'px';
}

window.addEventListener('resize', updateCanvasSize);

// ===== 坐标映射：触摸点 → Mac 屏幕坐标 =====
function mapToScreen(touchX, touchY) {
    const rect = state.canvas.getBoundingClientRect();
    const relX = (touchX - rect.left) / rect.width;
    const relY = (touchY - rect.top) / rect.height;
    return {
        x: Math.round(relX * state.screenWidth),
        y: Math.round(relY * state.screenHeight),
    };
}

// ===== 发送操控指令 =====
function sendAction(action) {
    if (state.controlWs && state.controlWs.readyState === WebSocket.OPEN) {
        state.controlWs.send(JSON.stringify(action));
    }
}

function sendKey(keys) {
    if (state.mode !== 'control') return;
    sendAction({ action: 'key', keys: keys });
}

function goHome() {
    if (state.mode !== 'control') return;
    sendKey(['command', 'space']);
}

// ===== 启动 =====
document.addEventListener('DOMContentLoaded', init);
