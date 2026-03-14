/**
 * touch.js - 触摸与手势处理模块
 * 浏览模式：单指平移、双指缩放（围绕中心点）、双击切换缩放
 * 操作模式：单击、双击、长按右键、双指滚动
 */

function setupTouchEvents() {
    const container = document.getElementById('screen-container');
    const canvas = document.getElementById('screen-canvas');
    let touchStartTime = 0;
    let touchStartPos = null;
    let longPressTimer = null;
    let isTouchMoved = false;
    let lastTapTime = 0;

    // 浏览模式状态
    let panStartView = null;      // 单指拖动开始时的 view 快照
    let panStartTouch = null;     // 单指拖动开始时的触摸位置
    let isPinching = false;
    let lastPinchDist = 0;
    let lastPinchMid = null;

    // ===== 工具函数 =====
    function getPinchDist(t1, t2) {
        const dx = t1.clientX - t2.clientX;
        const dy = t1.clientY - t2.clientY;
        return Math.sqrt(dx * dx + dy * dy);
    }

    function getPinchMid(t1, t2) {
        return { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 };
    }

    function applyTransform() {
        const v = state.view;
        state.canvas.style.transform = `translate(${v.translateX}px, ${v.translateY}px) scale(${v.scale})`;
    }

    /**
     * 获取 canvas 在容器中居中后的 left/top 偏移
     * (flex居中导致canvas并非在0,0)
     */
    function getCanvasBaseOffset() {
        const ct = document.getElementById('screen-container');
        const cw = ct.clientWidth;
        const ch = ct.clientHeight;
        return {
            x: (cw - state.canvasDisplayW) / 2,
            y: (ch - state.canvasDisplayH) / 2,
        };
    }

    function clampView() {
        const v = state.view;
        if (v.scale < 1) v.scale = 1;
        if (v.scale > 5) v.scale = 5;

        const ct = document.getElementById('screen-container');
        const cw = ct.clientWidth;
        const ch = ct.clientHeight;
        const scaledW = state.canvasDisplayW * v.scale;
        const scaledH = state.canvasDisplayH * v.scale;
        const base = getCanvasBaseOffset();

        if (scaledW <= cw) {
            v.translateX = 0;
        } else {
            const minX = cw - base.x - scaledW;
            const maxX = base.x;
            v.translateX = Math.max(minX, Math.min(maxX, v.translateX));
        }
        if (scaledH <= ch) {
            v.translateY = 0;
        } else {
            const minY = ch - base.y - scaledH;
            const maxY = base.y;
            v.translateY = Math.max(minY, Math.min(maxY, v.translateY));
        }
    }

    /**
     * 围绕屏幕上某个焦点(fx,fy)进行缩放
     * 焦点在缩放前后保持在屏幕同一位置
     */
    function zoomAroundPoint(newScale, fx, fy) {
        const v = state.view;
        const oldScale = v.scale;
        const base = getCanvasBaseOffset();

        // 焦点在 canvas 未缩放坐标系中的位置
        // screenX = base.x + translateX + canvasX * scale
        // canvasX = (fx - base.x - translateX) / oldScale
        const canvasX = (fx - base.x - v.translateX) / oldScale;
        const canvasY = (fy - base.y - v.translateY) / oldScale;

        v.scale = newScale;

        // 让焦点在新缩放下仍然对应到屏幕 (fx, fy)
        v.translateX = fx - base.x - canvasX * newScale;
        v.translateY = fy - base.y - canvasY * newScale;
    }

    // ===== touchstart =====
    container.addEventListener('touchstart', (e) => {
        e.preventDefault();
        const touch = e.touches[0];
        touchStartTime = Date.now();
        touchStartPos = { x: touch.clientX, y: touch.clientY };
        isTouchMoved = false;

        if (state.mode === 'browse') {
            if (e.touches.length === 2) {
                isPinching = true;
                lastPinchDist = getPinchDist(e.touches[0], e.touches[1]);
                lastPinchMid = getPinchMid(e.touches[0], e.touches[1]);
            } else {
                isPinching = false;
                panStartView = { ...state.view };
                panStartTouch = { x: touch.clientX, y: touch.clientY };
            }
        } else {
            // 操作模式：长按检测 → 右键
            longPressTimer = setTimeout(() => {
                if (!isTouchMoved) {
                    const pos = mapToScreen(touch.clientX, touch.clientY);
                    sendAction({ action: 'right_click', x: pos.x, y: pos.y });
                    showRipple(touch.clientX, touch.clientY, '#ff9500');
                    touchStartPos = null;
                }
            }, 600);
        }
    }, { passive: false });

    // ===== touchmove =====
    container.addEventListener('touchmove', (e) => {
        e.preventDefault();

        if (state.mode === 'browse') {
            if (e.touches.length === 2) {
                isTouchMoved = true;

                const dist = getPinchDist(e.touches[0], e.touches[1]);
                const mid = getPinchMid(e.touches[0], e.touches[1]);

                if (!isPinching) {
                    // 从单指切换到双指，初始化 pinch 状态
                    isPinching = true;
                    lastPinchDist = dist;
                    lastPinchMid = mid;
                    return;
                }

                // 增量缩放：基于上一帧计算
                const scaleRatio = dist / lastPinchDist;
                const newScale = state.view.scale * scaleRatio;

                // 围绕当前双指中点缩放
                zoomAroundPoint(newScale, mid.x, mid.y);

                // 双指平移（中点偏移）
                state.view.translateX += mid.x - lastPinchMid.x;
                state.view.translateY += mid.y - lastPinchMid.y;

                clampView();
                applyTransform();

                // 更新上一帧状态
                lastPinchDist = dist;
                lastPinchMid = mid;

            } else if (e.touches.length === 1 && !isPinching) {
                // 单指拖动
                const touch = e.touches[0];
                const dx = touch.clientX - panStartTouch.x;
                const dy = touch.clientY - panStartTouch.y;
                if (Math.abs(dx) > 5 || Math.abs(dy) > 5) {
                    isTouchMoved = true;
                }
                if (isTouchMoved && panStartView) {
                    state.view.translateX = panStartView.translateX + dx;
                    state.view.translateY = panStartView.translateY + dy;
                    clampView();
                    applyTransform();
                }
            }
        } else {
            // 操作模式
            const touch = e.touches[0];
            if (touchStartPos) {
                const dx = touch.clientX - touchStartPos.x;
                const dy = touch.clientY - touchStartPos.y;
                if (Math.abs(dx) > 10 || Math.abs(dy) > 10) {
                    isTouchMoved = true;
                    clearTimeout(longPressTimer);
                }
            }
            // 双指滚动
            if (e.touches.length === 2) {
                clearTimeout(longPressTimer);
                const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
                if (touchStartPos && touchStartPos._lastScrollY) {
                    const deltaY = midY - touchStartPos._lastScrollY;
                    if (Math.abs(deltaY) > 5) {
                        const pos = mapToScreen(e.touches[0].clientX, e.touches[0].clientY);
                        sendAction({
                            action: 'scroll',
                            x: pos.x, y: pos.y,
                            direction: deltaY > 0 ? 'down' : 'up',
                            amount: Math.min(Math.abs(Math.round(deltaY / 10)), 10),
                        });
                    }
                }
                if (touchStartPos) touchStartPos._lastScrollY = midY;
            }
        }
    }, { passive: false });

    // ===== touchend =====
    container.addEventListener('touchend', (e) => {
        e.preventDefault();
        clearTimeout(longPressTimer);

        if (state.mode === 'browse') {
            if (isPinching && e.touches.length === 1) {
                // 双指松开一根 → 切换为单指拖动，重新建立快照防止跳变
                isPinching = false;
                panStartView = { ...state.view };
                panStartTouch = { x: e.touches[0].clientX, y: e.touches[0].clientY };
                return;
            }

            if (e.touches.length === 0) {
                isPinching = false;

                // 双击还原/放大
                if (!isTouchMoved) {
                    const now = Date.now();
                    const elapsed = now - touchStartTime;
                    if (elapsed < 300 && now - lastTapTime < 300) {
                        if (state.view.scale > 1.1) {
                            state.view = { scale: 1, translateX: 0, translateY: 0 };
                        } else {
                            // 以触摸点为中心放大2x
                            zoomAroundPoint(2, touchStartPos.x, touchStartPos.y);
                            clampView();
                        }
                        applyTransform();
                        lastTapTime = 0;
                    } else {
                        lastTapTime = now;
                    }
                }
            }
            return;
        }

        // 操作模式
        if (!touchStartPos || isTouchMoved) return;

        const elapsed = Date.now() - touchStartTime;
        if (elapsed >= 600) return;

        const pos = mapToScreen(touchStartPos.x, touchStartPos.y);
        const now = Date.now();

        if (now - lastTapTime < 300) {
            sendAction({ action: 'double_click', x: pos.x, y: pos.y });
            showRipple(touchStartPos.x, touchStartPos.y, '#30d158');
            lastTapTime = 0;
        } else {
            sendAction({ action: 'click', x: pos.x, y: pos.y });
            showRipple(touchStartPos.x, touchStartPos.y);
            lastTapTime = now;
        }
    }, { passive: false });

    // 桌面浏览器鼠标事件（仅操作模式）
    canvas.addEventListener('click', (e) => {
        if ('ontouchstart' in window) return;
        if (state.mode !== 'control') return;
        const pos = mapToScreen(e.clientX, e.clientY);
        sendAction({ action: 'click', x: pos.x, y: pos.y });
        showRipple(e.clientX, e.clientY);
    });
}
