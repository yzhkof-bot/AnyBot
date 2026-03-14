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
    // 浏览模式的变换状态
    view: { scale: 1, translateX: 0, translateY: 0 },
    // 窗口模式
    currentWindowId: null,          // 当前选中的窗口 ID（null=全屏）
    windowViewCache: {},            // 每个窗口/全屏的缩放位置记忆: { windowId: {scale, translateX, translateY} }
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
    setupScrollPanel();
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
    pc.addEventListener('connectionstatechange', () => {
        console.log('[WebRTC] 连接状态:', pc.connectionState);
        if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
            handleWebRTCDisconnect();
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
        }
    } catch (e) {
        // 忽略
    }
}

/**
 * WebRTC 断开后自动回退到 MJPEG
 */
function handleWebRTCDisconnect() {
    console.warn('[WebRTC] 连接断开，回退到 MJPEG...');
    cleanupWebRTC();

    state.streamMode = 'mjpeg';
    const host = location.host || 'localhost:8765';
    const wsProtocol = location.protocol === 'https:' ? 'wss' : 'ws';
    connectScreenWs(`${wsProtocol}://${host}/ws/screen`);
    connectControlWs(`${wsProtocol}://${host}/ws/control`);
    setStatus(true, '已连接 (MJPEG回退)');
}

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
    if (typeof updateCursorIndicator === 'function') {
        updateCursorIndicator(msg.x, msg.y);
    }
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
    return {
        x: Math.round(relX * state.screenWidth),
        y: Math.round(relY * state.screenHeight),
    };
}

// ===== 发送操控指令 =====
/**
 * 统一发送控制指令
 * WebRTC 模式 → DataChannel (延迟更低)
 * MJPEG 模式 → WebSocket
 */
function sendAction(action) {
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
}

// ===== 启动 =====
document.addEventListener('DOMContentLoaded', init);
