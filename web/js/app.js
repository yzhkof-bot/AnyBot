/**
 * app.js - 核心应用模块
 * 全局状态、初始化、连接管理、画面适配、坐标映射
 *
 * 传输模式:
 *  - WebRTC (默认): H.264 视频流，画质好、带宽低、延迟低
 *  - MJPEG (回退):  WebSocket + 逐帧 JPEG，兼容性好
 *
 * 控制指令优先级:
 *  - RTCDataChannel (WebRTC 模式): 延迟最低，走 SCTP over DTLS
 *  - WebSocket (回退): 独立 /ws/control 通道
 */

// ===== 全局状态 =====
const state = {
    // 传输模式: 'webrtc' | 'mjpeg'
    streamMode: 'webrtc',
    // WebRTC 相关
    pc: null,               // RTCPeerConnection
    dataChannel: null,       // RTCDataChannel (控制指令)
    videoEl: null,           // <video> 元素 (WebRTC 播放)
    // MJPEG 回退 (WebSocket)
    screenWs: null,
    controlWs: null,
    // 通用
    connected: false,
    canvas: null,
    ctx: null,
    screenWidth: 0,     // Mac 屏幕实际宽度
    screenHeight: 0,    // Mac 屏幕实际高度
    canvasDisplayW: 0,  // 显示宽度
    canvasDisplayH: 0,  // 显示高度
    imageWidth: 0,      // 画面宽度
    imageHeight: 0,     // 画面高度
    frameCount: 0,
    lastFpsTime: 0,
    bytesReceived: 0,
    keyboardOpen: false,
    // 模式: 'browse' | 'control'
    mode: 'browse',
    // 滚轮模式（独立开关，优先于 browse/control）
    scrollMode: false,
    // 浏览模式的变换状态
    view: { scale: 1, translateX: 0, translateY: 0 },
    // 窗口模式
    currentWindowId: null,          // 当前选中的窗口 ID（null=全屏）
    _windowBounds: null,            // 当前窗口的屏幕位置 {x, y, w, h}（光标映射用）
    windowViewCache: {},            // 每个窗口/全屏的缩放位置记忆: { windowId: {scale, translateX, translateY} }
    windowSwitching: false,         // 窗口切换过渡中（等待视频帧更新，期间禁止操控）
    // 渲染循环标志
    _renderLoopRunning: false,
    // 统计
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

    // 创建隐藏的 <video> 元素用于 WebRTC 播放
    state.videoEl = document.createElement('video');
    state.videoEl.autoplay = true;
    state.videoEl.playsInline = true;
    state.videoEl.muted = true;
    state.videoEl.style.display = 'none';
    document.body.appendChild(state.videoEl);

    setupTouchEvents();
    setupKeyboardInput();
}

// ===== 连接入口 =====
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

        // 尝试 WebRTC 连接
        try {
            await startWebRTC(host, protocol);
            state.streamMode = 'webrtc';
            console.log('[AnyBot] WebRTC 连接成功 (H.264 视频流)');
        } catch (rtcErr) {
            // WebRTC 失败，回退到 MJPEG
            console.warn('[AnyBot] WebRTC 失败，回退到 MJPEG:', rtcErr);
            state.streamMode = 'mjpeg';
            const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
            connectScreenWs(`${wsProtocol}://${host}/ws/screen`);
            connectControlWs(`${wsProtocol}://${host}/ws/control`);
        }

        document.getElementById('connect-overlay').classList.add('hidden');
        setStatus(true, state.streamMode === 'webrtc' ? '已连接 (WebRTC)' : '已连接 (MJPEG)');

        // 恢复之前保存的客户端状态
        await restoreClientState();
    } catch (e) {
        info.textContent = `连接失败: ${e.message}`;
        btn.disabled = false;
        btn.textContent = '重新连接';
    }
}


// ═══════════════════════════════════════════════
//  WebRTC 模式
// ═══════════════════════════════════════════════

async function startWebRTC(host, protocol) {
    const pc = new RTCPeerConnection({
        iceServers: [],  // 局域网直连，不需要 STUN/TURN
    });
    state.pc = pc;
    state._lastWebrtcBytes = 0;

    // 接收服务端推送的视频轨道
    pc.addEventListener('track', (event) => {
        console.log('[WebRTC] 收到视频轨道:', event.track.kind);
        if (event.track.kind === 'video') {
            const stream = event.streams[0] || new MediaStream([event.track]);
            state.videoEl.srcObject = stream;

            // 显式调用 play()（移动端 Safari 需要）
            const playPromise = state.videoEl.play();
            if (playPromise) {
                playPromise.catch(e => console.warn('[WebRTC] video.play() 失败:', e));
            }

            // 用轮询方式等待视频尺寸就绪（比 loadedmetadata 更可靠）
            waitForVideoReady();
        }
    });

    // 创建 DataChannel 用于控制指令
    const dc = pc.createDataChannel('control', { ordered: true });
    state.dataChannel = dc;

    dc.addEventListener('open', () => {
        console.log('[WebRTC] DataChannel 已打开');
        // 启动心跳
        startDataChannelHeartbeat();
    });

    dc.addEventListener('message', (event) => {
        handleDataChannelMessage(event.data);
    });

    dc.addEventListener('close', () => {
        console.log('[WebRTC] DataChannel 已关闭');
    });

    // 监听连接状态
    let _disconnectTimer = null;
    pc.addEventListener('connectionstatechange', () => {
        console.log('[WebRTC] 连接状态:', pc.connectionState);
        clearTimeout(_disconnectTimer);
        if (pc.connectionState === 'failed') {
            handleWebRTCDisconnect();
        } else if (pc.connectionState === 'disconnected') {
            // disconnected 可能是暂时的，等 2 秒看是否恢复
            _disconnectTimer = setTimeout(() => {
                if (pc.connectionState === 'disconnected') {
                    handleWebRTCDisconnect();
                }
            }, 2000);
        }
    });

    // 需要添加一个 transceiver 来请求接收视频
    // 因为是服务端推流（server → client），客户端需要声明 recvonly
    pc.addTransceiver('video', { direction: 'recvonly' });

    // 创建 Offer
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // 等待 ICE 候选收集完成
    await waitForIceGathering(pc);

    // 发送 Offer 到服务端，获取 Answer
    const resp = await fetch(`${protocol}://${host}/api/webrtc/offer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
        }),
    });

    if (!resp.ok) {
        throw new Error(`WebRTC 信令失败: ${resp.status}`);
    }

    const answer = await resp.json();
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
    console.log('[WebRTC] SDP 交换完成，等待连接建立...');
}

/**
 * 轮询等待 <video> 的 videoWidth/videoHeight 就绪
 * 比 loadedmetadata 事件更可靠（避免事件在绑定前已触发的问题）
 */
function waitForVideoReady() {
    let attempts = 0;
    const maxAttempts = 100; // 最多等 5 秒

    function check() {
        attempts++;
        const vw = state.videoEl.videoWidth;
        const vh = state.videoEl.videoHeight;

        if (vw > 0 && vh > 0) {
            console.log(`[WebRTC] 视频就绪: ${vw}x${vh}`);
            state.imageWidth = vw;
            state.imageHeight = vh;
            state.canvas.width = vw;
            state.canvas.height = vh;
            updateCanvasSize();
            startVideoRenderLoop();
            return;
        }

        if (attempts < maxAttempts) {
            setTimeout(check, 50);
        } else {
            console.warn('[WebRTC] 视频尺寸等待超时，尝试启动渲染循环');
            startVideoRenderLoop();
        }
    }
    check();
}

/**
 * 等待 ICE 候选收集完成
 */
function waitForIceGathering(pc) {
    return new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') {
            resolve();
            return;
        }
        const checkState = () => {
            if (pc.iceGatheringState === 'complete') {
                pc.removeEventListener('icegatheringstatechange', checkState);
                resolve();
            }
        };
        pc.addEventListener('icegatheringstatechange', checkState);
        // 超时保护：3 秒后强制继续
        setTimeout(resolve, 3000);
    });
}

/**
 * 将 <video> 的画面实时绘制到 <canvas>
 * 这样触摸事件仍然绑定在 canvas 上，与 MJPEG 模式一致
 */
function startVideoRenderLoop() {
    if (state._renderLoopRunning) return; // 防止重复启动
    state._renderLoopRunning = true;

    function render() {
        // 模式切换后停止循环
        if (state.streamMode !== 'webrtc') {
            state._renderLoopRunning = false;
            return;
        }

        const vw = state.videoEl.videoWidth;
        const vh = state.videoEl.videoHeight;

        // 视频尚未就绪，跳过本帧但不中断循环
        if (!vw || !vh) {
            requestAnimationFrame(render);
            return;
        }

        // 检查视频尺寸是否变化
        if (state.canvas.width !== vw || state.canvas.height !== vh) {
            state.canvas.width = vw;
            state.canvas.height = vh;
            state.imageWidth = vw;
            state.imageHeight = vh;
            updateCanvasSize();

            // 视频帧尺寸变化 → 新窗口画面已到达，解除切换锁定
            if (state.windowSwitching) {
                state.windowSwitching = false;
                console.log(`[AnyBot] 视频帧已更新为 ${vw}x${vh}，窗口切换完成`);
            }
        }

        // video → canvas
        state.ctx.drawImage(state.videoEl, 0, 0);
        updateFps();
        requestAnimationFrame(render);
    }
    requestAnimationFrame(render);
}

/**
 * DataChannel 心跳（测量 RTT）
 */
function startDataChannelHeartbeat() {
    setInterval(() => {
        if (state.dataChannel && state.dataChannel.readyState === 'open') {
            state.dataChannel.send(JSON.stringify({
                type: 'ping',
                ts: Date.now(),
            }));
        }
    }, 5000);
}

/**
 * 处理 DataChannel 收到的消息
 */
function handleDataChannelMessage(data) {
    try {
        const msg = JSON.parse(data);
        if (msg.type === 'pong') {
            const rtt = Date.now() - msg.ts;
            const fpsEl = document.getElementById('fps-text');
            if (fpsEl) fpsEl.dataset.rtt = rtt;
        } else if (msg.type === 'cursor') {
            handleCursorMessage(msg);
        }
    } catch (e) {
        // 忽略
    }
}

/**
 * WebRTC 断开后尝试重连，重连失败才回退到 MJPEG
 */
let _webrtcReconnecting = false;

function handleWebRTCDisconnect() {
    if (_webrtcReconnecting) return;  // 防止重复触发
    console.warn('[WebRTC] 连接断开，尝试重连...');
    cleanupWebRTC();
    setStatus(false, '重连中...');
    reconnectWebRTC();
}

async function reconnectWebRTC(retries = 3, delay = 1000) {
    _webrtcReconnecting = true;
    const host = location.host || 'localhost:8765';
    const protocol = location.protocol === 'https:' ? 'https' : 'http';

    for (let i = 1; i <= retries; i++) {
        // 页面隐藏时暂停重连，等 visibilitychange 再触发
        if (document.hidden) {
            console.log('[WebRTC] 页面不可见，等待恢复后重连');
            _webrtcReconnecting = false;
            return;
        }
        try {
            console.log(`[WebRTC] 重连尝试 ${i}/${retries}...`);
            setStatus(false, `重连中 (${i}/${retries})...`);
            await startWebRTC(host, protocol);
            state.streamMode = 'webrtc';
            setStatus(true, '已连接 (WebRTC)');
            console.log('[WebRTC] 重连成功');
            _webrtcReconnecting = false;
            return;
        } catch (e) {
            console.warn(`[WebRTC] 重连失败 (${i}/${retries}):`, e);
            cleanupWebRTC();
            if (i < retries) {
                await new Promise(r => setTimeout(r, delay));
            }
        }
    }

    // 全部失败，回退到 MJPEG
    console.warn('[WebRTC] 重连全部失败，回退到 MJPEG');
    state.streamMode = 'mjpeg';
    const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
    connectScreenWs(`${wsProtocol}://${host}/ws/screen`);
    connectControlWs(`${wsProtocol}://${host}/ws/control`);
    setStatus(true, '已连接 (MJPEG回退)');
    _webrtcReconnecting = false;
}

// 页面从最小化/后台恢复时，如果 WebRTC 已断开则自动重连
document.addEventListener('visibilitychange', () => {
    if (document.hidden || !state.connected) return;
    if (state.streamMode === 'webrtc' && state.pc && state.pc.connectionState === 'connected') return;
    // WebRTC 不可用，尝试重连
    if (!_webrtcReconnecting) {
        console.log('[AnyBot] 页面恢复可见，WebRTC 已断开，尝试重连...');
        cleanupWebRTC();
        reconnectWebRTC();
    }
});

/**
 * 清理 WebRTC 资源
 */
function cleanupWebRTC() {
    state._renderLoopRunning = false;
    if (state.dataChannel) {
        state.dataChannel.close();
        state.dataChannel = null;
    }
    if (state.pc) {
        state.pc.close();
        state.pc = null;
    }
    if (state.videoEl) {
        state.videoEl.srcObject = null;
    }
}


// ═══════════════════════════════════════════════
//  MJPEG 回退模式 (WebSocket)
// ═══════════════════════════════════════════════

function connectScreenWs(url) {
    state.screenWs = new WebSocket(url);
    state.screenWs.binaryType = 'blob';

    state.screenWs.onmessage = (event) => {
        // 统计带宽
        if (typeof event.data === 'string') {
            state.bytesReceived += event.data.length;
            handleTextMessage(event.data);
        } else {
            state.bytesReceived += event.data.size || 0;
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
        if (state.canvas.width !== img.width || state.canvas.height !== img.height) {
            state.canvas.width = img.width;
            state.canvas.height = img.height;
            state.imageWidth = img.width;
            state.imageHeight = img.height;
            updateCanvasSize();

            // 帧尺寸变化 → 新窗口画面已到达，解除切换锁定
            if (state.windowSwitching) {
                state.windowSwitching = false;
                console.log(`[AnyBot] MJPEG 帧已更新为 ${img.width}x${img.height}，窗口切换完成`);
            }
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
                updateFps();
                break;

            default:
                break;
        }
    } catch (e) {
        // JSON 解析失败，忽略
    }
}

function handleCursorMessage(msg) {
    // 保存窗口 bounds（窗口模式下光标坐标转换需要）
    if (msg.bounds) {
        state._windowBounds = msg.bounds;
    } else if (state.currentWindowId === null) {
        state._windowBounds = null;
    }
    updateCursorIndicator(msg.x, msg.y);
    if (msg.fps !== undefined) {
        const fpsEl = document.getElementById('fps-text');
        if (fpsEl) fpsEl.dataset.serverFps = msg.fps;
    }
}

function handleDeltaFrame(msg) {
    state.delta.deltaFrames++;

    if (!msg.regions || msg.regions.length === 0) {
        updateFps();
        return;
    }

    let loadedCount = 0;
    const total = msg.regions.length;

    msg.regions.forEach((region) => {
        const img = new Image();
        img.onload = () => {
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


// ═══════════════════════════════════════════════
//  通用功能
// ═══════════════════════════════════════════════

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

    // 窗口模式下使用 imageWidth/imageHeight（和视频帧同步）来映射。
    // 原因：切换窗口时 selectWindow() 会立即更新 screenWidth/Height 为
    // 新窗口尺寸，但 WebRTC 视频帧需要时间更新，canvas 上仍显示旧画面。
    // 如果新旧窗口尺寸不同（如两个不同大小的终端窗口），坐标映射就会错乱。
    // imageWidth/imageHeight 始终与 canvas 显示内容保持同步，避免了此问题。
    //
    // 全屏模式下仍用 screenWidth/screenHeight（逻辑像素），因为全屏时
    // 视频帧尺寸可能被 thumbnail 缩放（如 Retina 屏 2880→1920），
    // 和 CGEvent 所需的逻辑像素坐标不一致。
    let mapW, mapH;
    if (state.currentWindowId !== null && state.imageWidth > 0) {
        mapW = state.imageWidth;
        mapH = state.imageHeight;
    } else {
        mapW = state.screenWidth;
        mapH = state.screenHeight;
    }

    return {
        x: Math.round(relX * mapW),
        y: Math.round(relY * mapH),
    };
}

// ===== 光标跟随：Mac 屏幕坐标 → 手机页面位置 =====

/** 光标自动隐藏定时器 */
let _cursorHideTimer = null;

/**
 * 将服务端推送的 Mac 光标坐标映射到手机页面上并显示红点
 *
 * 映射过程（mapToScreen 的反向）：
 *   Mac坐标 → 归一化(0~1) → canvas 视口坐标 → 加上缩放/平移变换 → fixed 页面坐标
 *
 * @param {number} macX - Mac 屏幕 X 坐标（全局）
 * @param {number} macY - Mac 屏幕 Y 坐标（全局）
 */
function updateCursorIndicator(macX, macY) {
    const el = document.getElementById('cursor-indicator');
    if (!el || !state.canvas) return;

    // Step 1: 确定映射用的分辨率（和 mapToScreen 保持一致）
    let mapW, mapH;
    if (state.currentWindowId !== null && state.imageWidth > 0) {
        mapW = state.imageWidth;
        mapH = state.imageHeight;
    } else {
        mapW = state.screenWidth;
        mapH = state.screenHeight;
    }
    if (mapW <= 0 || mapH <= 0) return;

    // 窗口模式下需要将全局坐标转换为窗口内相对坐标
    // 服务端推送的是 pyautogui.position() 的全局坐标
    let relX, relY;
    if (state.currentWindowId !== null && state._windowBounds) {
        const wb = state._windowBounds;
        relX = (macX - wb.x) / wb.w;
        relY = (macY - wb.y) / wb.h;
    } else {
        // 全屏模式：直接归一化
        relX = macX / mapW;
        relY = macY / mapH;
    }

    // 光标在可视区域外则隐藏
    if (relX < -0.02 || relX > 1.02 || relY < -0.02 || relY > 1.02) {
        el.classList.remove('visible');
        return;
    }

    // Step 2: 归一化坐标 → Canvas 上的 CSS 像素位置
    const rect = state.canvas.getBoundingClientRect();
    const pageX = rect.left + relX * rect.width;
    const pageY = rect.top + relY * rect.height;

    // Step 3: 设置位置并显示（CSS transform: translate(-50%,-50%) 居中）
    el.style.left = pageX + 'px';
    el.style.top = pageY + 'px';
    el.classList.add('visible');

    // 2 秒无更新自动隐藏
    clearTimeout(_cursorHideTimer);
    _cursorHideTimer = setTimeout(() => {
        el.classList.remove('visible');
    }, 2000);
}

// ===== 发送操控指令 =====
/**
 * 统一发送控制指令
 * WebRTC 模式 → DataChannel (延迟更低)
 * MJPEG 模式 → WebSocket
 *
 * 窗口切换过渡期间，操控指令被静默丢弃（防止坐标错乱）
 */
function sendAction(action) {
    // 窗口切换过渡期：新画面尚未到达，坐标映射不可靠
    if (state.windowSwitching) {
        console.log('[AnyBot] 窗口切换中，丢弃操控指令:', action.action);
        return;
    }

    const msg = JSON.stringify(action);

    // 优先使用 DataChannel
    if (state.dataChannel && state.dataChannel.readyState === 'open') {
        state.dataChannel.send(msg);
        return;
    }

    // 回退到 WebSocket
    if (state.controlWs && state.controlWs.readyState === WebSocket.OPEN) {
        state.controlWs.send(msg);
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

// ===== 客户端状态持久化 (localStorage) =====
const STORAGE_KEY = 'anybot_state';

/**
 * 保存需要持久化的客户端状态到 localStorage
 * 包括：缩放位置缓存、当前窗口 ID、操作/浏览模式
 */
function saveClientState() {
    try {
        const data = {
            windowViewCache: state.windowViewCache,
            currentWindowId: state.currentWindowId,
            mode: state.mode,
            scrollMode: state.scrollMode,
        };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch (e) {
        // localStorage 不可用或配额满，静默忽略
    }
}

/**
 * 从 localStorage 恢复客户端状态
 * 在连接建立后调用，恢复缩放位置和窗口选择
 */
function loadClientState() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        return JSON.parse(raw);
    } catch (e) {
        return null;
    }
}

/**
 * 连接建立后，恢复之前保存的客户端状态
 * 1. 恢复窗口缩放位置缓存
 * 2. 恢复之前选中的窗口（通过服务端 API 重新选择）
 * 3. 恢复操作/浏览模式
 */
async function restoreClientState() {
    const saved = loadClientState();
    if (!saved) return;

    // 1. 恢复缩放位置缓存
    if (saved.windowViewCache && typeof saved.windowViewCache === 'object') {
        state.windowViewCache = saved.windowViewCache;
    }

    // 2. 恢复之前选中的窗口
    if (saved.currentWindowId !== null && saved.currentWindowId !== undefined) {
        try {
            // 先查询服务端当前的窗口列表，确认窗口还存在
            const resp = await fetch('/api/windows');
            const data = await resp.json();
            const windows = data.windows || [];
            const targetWin = windows.find(w => w.id === saved.currentWindowId);

            if (targetWin) {
                // 窗口仍然存在，重新选择它
                await selectWindow(targetWin.id, targetWin.name, targetWin.owner);
                console.log(`[AnyBot] 已恢复窗口: [${targetWin.owner}] ${targetWin.name}`);
            } else {
                // 窗口已不存在，尝试通过 owner+name 匹配找到类似的窗口
                console.log('[AnyBot] 之前的窗口已不存在，使用全屏模式');
            }
        } catch (e) {
            console.warn('[AnyBot] 恢复窗口状态失败:', e);
        }
    }

    // 3. 恢复缩放位置（根据当前窗口 ID）
    restoreViewFromCache(state.currentWindowId);

    // 4. 恢复操作/浏览模式
    if (saved.mode === 'control' || saved.mode === 'browse') {
        state.mode = saved.mode;
        updateModeUI();
    }

    // 5. 恢复滚轮模式
    if (saved.scrollMode) {
        state.scrollMode = true;
        updateScrollModeUI();
    }
}

// ===== 启动 =====
document.addEventListener('DOMContentLoaded', init);
