/**
 * AnyBot Agent Panel — AI Agent 聊天面板
 *
 * 功能：
 * - WebSocket 连接 /ws/agent，与 Agent 后端通信
 * - 消息列表渲染（用户消息、AI 回复、操作步骤、截图）
 * - Agent 状态显示（空闲/运行中/暂停/已停止）
 * - 暂停/恢复/停止控制
 */

// ───────── 状态 ─────────

let agentWs = null;
let agentState = 'idle';  // idle | running | paused | stopped
let agentPanelOpen = false;
let agentModels = [];       // 可用模型列表
let agentCurrentModel = ''; // 当前选中的模型 ID

// ───────── DOM 引用 ─────────

const agentOverlay   = () => document.getElementById('agent-overlay');
const agentPanel     = () => document.getElementById('agent-panel');
const agentMessages  = () => document.getElementById('agent-messages');
const agentInput     = () => document.getElementById('agent-input');
const agentSendBtn   = () => document.getElementById('agent-send-btn');
const agentStatusDot = () => document.getElementById('agent-status-dot');
const agentStatusTxt = () => document.getElementById('agent-status-text');
const agentControls  = () => document.getElementById('agent-controls');
const agentModelSel  = () => document.getElementById('agent-model-select');

// ───────── 面板开关 ─────────

function toggleAgentPanel() {
    if (agentPanelOpen) {
        closeAgentPanel();
    } else {
        openAgentPanel();
    }
}

function openAgentPanel() {
    agentPanelOpen = true;
    const overlay = agentOverlay();
    const panel = agentPanel();
    if (overlay) overlay.classList.add('show');
    if (panel) panel.classList.add('show');
    connectAgent();
    // 聚焦输入框
    setTimeout(() => {
        const input = agentInput();
        if (input && agentState !== 'running') input.focus();
    }, 350);
}

function closeAgentPanel() {
    agentPanelOpen = false;
    const overlay = agentOverlay();
    const panel = agentPanel();
    if (overlay) overlay.classList.remove('show');
    if (panel) panel.classList.remove('show');
}

// ───────── WebSocket 连接 ─────────

function connectAgent() {
    if (agentWs && agentWs.readyState <= 1) return; // 已连接或连接中

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws/agent`;

    agentWs = new WebSocket(url);

    agentWs.onopen = () => {
        console.log('[Agent] WebSocket 已连接');
        updateAgentStatus('idle');
        // 请求模型列表
        agentWs.send(JSON.stringify({ type: 'get_models' }));
    };

    agentWs.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleAgentMessage(msg);
        } catch (e) {
            console.error('[Agent] 解析消息失败:', e);
        }
    };

    agentWs.onclose = () => {
        console.log('[Agent] WebSocket 已断开');
        agentWs = null;
        updateAgentStatus('idle');
    };

    agentWs.onerror = (err) => {
        console.error('[Agent] WebSocket 错误:', err);
    };
}

// ───────── 消息处理 ─────────

function handleAgentMessage(msg) {
    const type = msg.type;

    // 更新状态
    if (msg.state) {
        updateAgentStatus(msg.state);
    }

    switch (type) {
        case 'state':
            // 纯状态更新，不添加消息
            if (msg.current_model) {
                agentCurrentModel = msg.current_model;
                updateModelSelect();
            }
            break;

        case 'state_info':
            // 状态详情，不显示
            break;

        case 'models':
            // 收到模型列表
            agentModels = msg.models || [];
            agentCurrentModel = msg.current || '';
            renderModelSelect();
            break;

        case 'model_changed':
            agentCurrentModel = msg.model;
            updateModelSelect();
            appendMessage('system', `已切换模型: ${msg.model_name}`);
            break;

        case 'text':
            appendMessage('ai', msg.content);
            break;

        case 'thinking':
            appendMessage('status', msg.content, 'thinking');
            break;

        case 'screenshot':
            if (msg.screenshot) {
                appendScreenshot(msg.screenshot, msg.content);
            } else {
                appendMessage('status', msg.content, 'screenshot');
            }
            break;

        case 'action':
            appendMessage('status', msg.content, 'action');
            break;

        case 'complete':
            appendMessage('system', msg.content, 'complete');
            break;

        case 'error':
            appendMessage('system', msg.content, 'error');
            break;

        case 'paused':
            appendMessage('system', msg.content, 'paused');
            break;

        case 'resumed':
            appendMessage('system', msg.content, 'resumed');
            break;

        default:
            console.log('[Agent] 未知消息类型:', type, msg);
    }
}

// ───────── 消息渲染 ─────────

function appendMessage(role, content, subtype) {
    const container = agentMessages();
    if (!container) return;

    const el = document.createElement('div');
    el.className = `agent-msg agent-msg-${role}`;
    if (subtype) el.classList.add(`agent-msg-${subtype}`);

    if (role === 'user') {
        el.innerHTML = `<div class="agent-msg-bubble user">${escapeHtml(content)}</div>`;
    } else if (role === 'ai') {
        el.innerHTML = `<div class="agent-msg-bubble ai">${formatAiText(content)}</div>`;
    } else if (role === 'status') {
        const icon = getStatusIcon(subtype);
        el.innerHTML = `<div class="agent-msg-step">${icon} ${escapeHtml(content)}</div>`;
    } else if (role === 'system') {
        const icon = getSystemIcon(subtype);
        el.innerHTML = `<div class="agent-msg-system ${subtype || ''}">${icon} ${escapeHtml(content)}</div>`;
    }

    container.appendChild(el);
    scrollToBottom();
}

function appendScreenshot(base64, caption) {
    const container = agentMessages();
    if (!container) return;

    const el = document.createElement('div');
    el.className = 'agent-msg agent-msg-screenshot';
    el.innerHTML = `
        <div class="agent-screenshot-wrap">
            <img class="agent-screenshot-thumb"
                 src="data:image/jpeg;base64,${base64}"
                 alt="屏幕截图"
                 onclick="this.classList.toggle('expanded')"
                 onerror="this.style.display='none'">
            ${caption ? `<div class="agent-screenshot-caption">${escapeHtml(caption)}</div>` : ''}
        </div>
    `;

    container.appendChild(el);
    scrollToBottom();
}

function getStatusIcon(subtype) {
    switch (subtype) {
        case 'thinking':  return '<span class="agent-icon thinking">🧠</span>';
        case 'screenshot': return '<span class="agent-icon">📸</span>';
        case 'action':    return '<span class="agent-icon">⚡</span>';
        default:          return '<span class="agent-icon">•</span>';
    }
}

function getSystemIcon(subtype) {
    switch (subtype) {
        case 'complete': return '✅';
        case 'error':    return '❌';
        case 'paused':   return '⏸️';
        case 'resumed':  return '▶️';
        default:         return 'ℹ️';
    }
}

function formatAiText(text) {
    // 简单的 markdown 风格渲染
    return escapeHtml(text)
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/`(.*?)`/g, '<code>$1</code>')
        .replace(/\n/g, '<br>');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function scrollToBottom() {
    const container = agentMessages();
    if (container) {
        requestAnimationFrame(() => {
            container.scrollTop = container.scrollHeight;
        });
    }
}

// ───────── 状态管理 ─────────

function updateAgentStatus(state) {
    agentState = state;

    const dot = agentStatusDot();
    const txt = agentStatusTxt();
    const controls = agentControls();
    const input = agentInput();
    const sendBtn = agentSendBtn();
    const modelSel = agentModelSel();

    // 状态指示灯
    if (dot) {
        dot.className = 'agent-status-dot ' + state;
    }

    // 状态文本
    const stateLabels = {
        idle: '空闲',
        running: '运行中',
        paused: '已暂停',
        stopped: '已停止',
    };
    if (txt) txt.textContent = stateLabels[state] || state;

    // 控制按钮可见性
    if (controls) {
        controls.style.display = (state === 'running' || state === 'paused') ? 'flex' : 'none';
    }

    // 输入框状态
    if (input) {
        input.disabled = (state === 'running');
        input.placeholder = state === 'running' ? 'Agent 正在执行...' : '输入任务，让 AI 帮你操作...';
    }
    if (sendBtn) {
        sendBtn.disabled = (state === 'running');
    }

    // 模型选择器（运行中禁用切换）
    if (modelSel) {
        modelSel.disabled = (state === 'running');
    }
}

// ───────── 用户操作 ─────────

function sendAgentMessage() {
    const input = agentInput();
    if (!input) return;

    const content = input.value.trim();
    if (!content) return;
    if (agentState === 'running') return;

    // 显示用户消息
    appendMessage('user', content);
    input.value = '';

    // 发送到后端
    if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        const payload = { type: 'chat', content };
        if (agentCurrentModel) {
            payload.model = agentCurrentModel;
        }
        agentWs.send(JSON.stringify(payload));
    } else {
        appendMessage('system', '未连接到 Agent 服务，请重新打开面板', 'error');
    }
}

function pauseAgent() {
    if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ type: 'pause' }));
    }
}

function resumeAgent() {
    if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ type: 'resume' }));
    }
}

function stopAgent() {
    if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ type: 'stop' }));
    }
}

// ───────── 模型选择 ─────────

function renderModelSelect() {
    const sel = agentModelSel();
    if (!sel) return;

    sel.innerHTML = '';
    
    // 按厂商分组
    const providers = {};
    agentModels.forEach(m => {
        if (!providers[m.provider]) providers[m.provider] = [];
        providers[m.provider].push(m);
    });

    for (const [provider, models] of Object.entries(providers)) {
        const group = document.createElement('optgroup');
        group.label = provider;
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.id;
            opt.textContent = m.name;
            if (m.context) {
                opt.textContent += ` (${Math.round(m.context / 1024)}K)`;
            }
            if (!m.computer_use) {
                opt.textContent += ' ⚠️';
                opt.title = '该模型不原生支持 Computer Use，效果可能受限';
            }
            if (m.id === agentCurrentModel) {
                opt.selected = true;
            }
            group.appendChild(opt);
        });
        sel.appendChild(group);
    }
}

function updateModelSelect() {
    const sel = agentModelSel();
    if (sel && agentCurrentModel) {
        sel.value = agentCurrentModel;
    }
}

function onModelChange(modelId) {
    if (!modelId) return;
    agentCurrentModel = modelId;
    
    // 通知后端
    if (agentWs && agentWs.readyState === WebSocket.OPEN) {
        agentWs.send(JSON.stringify({ type: 'set_model', model: modelId }));
    }
}

// ───────── 键盘事件 ─────────

document.addEventListener('DOMContentLoaded', () => {
    // 输入框回车发送
    const input = document.getElementById('agent-input');
    if (input) {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendAgentMessage();
            }
            // 阻止冒泡，防止影响远程控制的键盘事件
            e.stopPropagation();
        });
        // 阻止输入框的触摸事件冒泡
        input.addEventListener('touchstart', (e) => e.stopPropagation());
    }

    // 面板区域阻止触摸事件冒泡（不影响远程控制）
    const panel = document.getElementById('agent-panel');
    if (panel) {
        ['touchstart', 'touchmove', 'touchend'].forEach(evt => {
            panel.addEventListener(evt, (e) => e.stopPropagation());
        });
    }
});
