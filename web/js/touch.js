/**
 * touch.js - 触摸与手势处理模块
 * 浏览模式：单指平移、双指缩放（围绕中心点）、双击切换缩放
 * 操作模式：单击、双击、长按右键、双指滚动、单指拖拽
 */

function setupTouchEvents() {
    const container = document.getElementById('screen-container');
    const canvas = document.getElementById('screen-canvas');
    let touchStartTime = 0;
    let touchStartPos = null;
    let longPressTimer = null;
    let isTouchMoved = false;
    let lastTapTime = 0;
    let _browseSingleTapTimer = null;  // 浏览模式单击延迟（等 300ms 排除双击）

    // 浏览模式状态
    let panStartView = null;      // 单指拖动开始时的 view 快照
    let panStartTouch = null;     // 单指拖动开始时的触摸位置
    let isPinching = false;
    let lastPinchDist = 0;
    let lastPinchMid = null;

    // 操作模式 - 拖拽状态
    let isDragging = false;           // 是否正在拖拽中
    let dragIndicator = null;         // 拖拽模式视觉指示器
    let lastDragSendTime = 0;         // 上次发送 drag_move 的时间戳
    const DRAG_MOVE_THRESHOLD = 8;    // 移动超过 8px 进入拖拽模式（区分点击和拖拽）
    const DRAG_SEND_INTERVAL = 30;    // drag_move 最小发送间隔 30ms（约 33fps）

    // 滚轮模式状态
    let scrollStartY = 0;
    let scrollStartX = 0;
    let scrollAccumY = 0;
    let scrollAccumX = 0;
    let scrollLastSendTime = 0;
    let scrollIndicator = null;
    const SCROLL_STEP = 15;           // 每 15px 触发一次滚动
    const SCROLL_SEND_INTERVAL = 50;  // 最小发送间隔 50ms

    /**
     * 创建/显示滚轮模式触摸指示器（四向箭头 + 中心圆点）
     */
    function showScrollIndicator(x, y) {
        if (!scrollIndicator) {
            scrollIndicator = document.createElement('div');
            scrollIndicator.className = 'scroll-touch-indicator';
            scrollIndicator.innerHTML =
                '<div class="scroll-arrow up"></div>' +
                '<div class="scroll-arrow down"></div>' +
                '<div class="scroll-arrow left"></div>' +
                '<div class="scroll-arrow right"></div>' +
                '<div class="scroll-center"></div>';
            document.body.appendChild(scrollIndicator);
        }
        scrollIndicator.style.left = x + 'px';
        scrollIndicator.style.top = y + 'px';
        scrollIndicator.style.display = 'block';
        // 重置所有箭头高亮
        scrollIndicator.querySelectorAll('.scroll-arrow').forEach(a => a.classList.remove('active'));
    }

    function updateScrollIndicatorDirection(dy, dx) {
        if (!scrollIndicator) return;
        const arrows = scrollIndicator.querySelectorAll('.scroll-arrow');
        arrows.forEach(a => a.classList.remove('active'));
        // 高亮主要滑动方向
        if (Math.abs(dy) >= Math.abs(dx)) {
            if (dy < -5) scrollIndicator.querySelector('.scroll-arrow.up').classList.add('active');
            else if (dy > 5) scrollIndicator.querySelector('.scroll-arrow.down').classList.add('active');
        } else {
            if (dx < -5) scrollIndicator.querySelector('.scroll-arrow.left').classList.add('active');
            else if (dx > 5) scrollIndicator.querySelector('.scroll-arrow.right').classList.add('active');
        }
    }

    function hideScrollIndicator() {
        if (scrollIndicator) {
            scrollIndicator.style.display = 'none';
        }
    }

    /**
     * 显示拖拽指示器（触摸点附近的蓝色圆环）
     */
    function showDragIndicator(x, y) {
        if (!dragIndicator) {
            dragIndicator = document.createElement('div');
            dragIndicator.className = 'drag-indicator';
            document.body.appendChild(dragIndicator);
        }
        dragIndicator.style.left = x + 'px';
        dragIndicator.style.top = y + 'px';
        dragIndicator.style.display = 'block';
    }

    function updateDragIndicator(x, y) {
        if (dragIndicator) {
            dragIndicator.style.left = x + 'px';
            dragIndicator.style.top = y + 'px';
        }
    }

    function hideDragIndicator() {
        if (dragIndicator) {
            dragIndicator.style.display = 'none';
        }
    }

    /**
     * 进入拖拽模式：发送 drag_start 并显示指示器
     */
    function enterDragMode(clientX, clientY) {
        isDragging = true;
        lastDragSendTime = Date.now();
        const pos = mapToScreen(clientX, clientY);
        sendAction({ action: 'drag_start', x: pos.x, y: pos.y });
        showDragIndicator(clientX, clientY);
        // 震动反馈（支持的设备）
        if (navigator.vibrate) navigator.vibrate(30);
    }

    /**
     * 结束拖拽模式：发送 drag_end 并隐藏指示器
     */
    function exitDragMode(clientX, clientY) {
        if (!isDragging) return;
        isDragging = false;
        const pos = mapToScreen(clientX, clientY);
        sendAction({ action: 'drag_end', x: pos.x, y: pos.y });
        hideDragIndicator();
    }

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

        // 统一公式：画面左边界不超出容器左边界，右边界不超出容器右边界
        // 画面左边界屏幕坐标 = base.x + translateX，需 >= 0  →  translateX >= -base.x
        // 画面右边界屏幕坐标 = base.x + translateX + scaledW，需 <= cw  →  translateX <= cw - base.x - scaledW
        const minX = Math.min(cw - base.x - scaledW, -base.x);
        const maxX = Math.max(cw - base.x - scaledW, -base.x);
        // 限制范围：但不能超出居中位置（画面小于容器时 minX > 0, maxX < 0 不合理，取 min/max 修正）
        // 当 scaledW <= cw 时：-base.x < 0, cw-base.x-scaledW = base.x > 0 → 范围 [-base.x, base.x] 没问题
        // 当 scaledW > cw 时：-base.x < 0, cw-base.x-scaledW < -base.x → min 是后者 → 也没问题
        v.translateX = Math.max(minX, Math.min(maxX, v.translateX));

        // Y 轴同理
        const minY = Math.min(ch - base.y - scaledH, -base.y);
        const maxY = Math.max(ch - base.y - scaledH, -base.y);
        v.translateY = Math.max(minY, Math.min(maxY, v.translateY));
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

        // 双指触摸 → 强制回到浏览模式（从任何模式）
        if (e.touches.length >= 2) {
            // 退出滚轮模式
            if (state.scrollMode) {
                state.scrollMode = false;
                hideScrollIndicator();
                updateScrollModeUI();
                if (typeof saveClientState === 'function') saveClientState();
            }
            // 退出操作模式
            if (state.mode === 'control') {
                // 结束正在进行的拖拽
                if (isDragging) {
                    exitDragMode(touch.clientX, touch.clientY);
                }
                clearTimeout(longPressTimer);
                state.mode = 'browse';
                updateModeUI();
                if (typeof saveClientState === 'function') saveClientState();
            }
            // 收起虚拟键盘
            if (state.keyboardOpen) {
                toggleKeyboard();
            }
            // 初始化浏览模式双指缩放
            isPinching = true;
            lastPinchDist = getPinchDist(e.touches[0], e.touches[1]);
            lastPinchMid = getPinchMid(e.touches[0], e.touches[1]);
            return;
        }

        // 滚轮模式优先：单指滑动 = 滚动
        if (state.scrollMode) {
            scrollStartY = touch.clientY;
            scrollStartX = touch.clientX;
            scrollAccumY = 0;
            scrollAccumX = 0;
            scrollLastSendTime = 0;
            isPinching = false;
            showScrollIndicator(touch.clientX, touch.clientY);
            return;
        }

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
            // 操作模式
            // 长按 600ms 且没移动 → 右键
            // 按住后移动超过阈值 → 进入拖拽（由 touchmove 处理）
            longPressTimer = setTimeout(() => {
                if (!isTouchMoved && !isDragging) {
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

        // 滚轮模式：单指滚动（双指已在 touchstart 切回浏览模式）
        if (state.scrollMode) {
            if (e.touches.length !== 1) return;
            const touch = e.touches[0];
            // 判断是否真正滑动（超过 5px 阈值才算，避免单击时微小抖动误判）
            if (touchStartPos) {
                const totalDx = touch.clientX - touchStartPos.x;
                const totalDy = touch.clientY - touchStartPos.y;
                if (Math.abs(totalDx) > 5 || Math.abs(totalDy) > 5) {
                    isTouchMoved = true;
                }
            }
            const deltaY = touch.clientY - scrollStartY;
            const deltaX = touch.clientX - scrollStartX;
            scrollStartY = touch.clientY;
            scrollStartX = touch.clientX;
            scrollAccumY += deltaY;
            scrollAccumX += deltaX;
            updateScrollIndicatorDirection(scrollAccumY, scrollAccumX);

            const now = Date.now();
            if (now - scrollLastSendTime < SCROLL_SEND_INTERVAL) return;

            const pos = mapToScreen(touchStartPos.x, touchStartPos.y);
            let sent = false;

            // 垂直滚动：Y 轴达到阈值且 Y 是主方向
            if (Math.abs(scrollAccumY) >= SCROLL_STEP && Math.abs(scrollAccumY) > Math.abs(scrollAccumX)) {
                const direction = scrollAccumY > 0 ? 'down' : 'up';
                const amount = Math.min(Math.round(Math.abs(scrollAccumY) / SCROLL_STEP), 8);
                sendAction({ action: 'scroll', x: pos.x, y: pos.y, direction, amount });
                scrollAccumY = 0;
                sent = true;
            }
            // 水平滚动：X 轴达到阈值且 X 是主方向
            if (!sent && Math.abs(scrollAccumX) >= SCROLL_STEP && Math.abs(scrollAccumX) > Math.abs(scrollAccumY)) {
                const direction = scrollAccumX > 0 ? 'left' : 'right';
                const amount = Math.min(Math.round(Math.abs(scrollAccumX) / SCROLL_STEP), 8);
                sendAction({ action: 'scroll', x: pos.x, y: pos.y, direction, amount });
                scrollAccumX = 0;
                sent = true;
            }
            if (sent) scrollLastSendTime = now;
            return;
        }

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

            if (e.touches.length === 1) {
                if (isDragging) {
                    // 拖拽中 → 节流发送 drag_move（限制频率，避免堆积）
                    const now = Date.now();
                    if (now - lastDragSendTime >= DRAG_SEND_INTERVAL) {
                        lastDragSendTime = now;
                        const pos = mapToScreen(touch.clientX, touch.clientY);
                        sendAction({ action: 'drag_move', x: pos.x, y: pos.y });
                    }
                    // 指示器始终跟随（不受节流影响）
                    updateDragIndicator(touch.clientX, touch.clientY);
                } else if (touchStartPos) {
                    const dx = touch.clientX - touchStartPos.x;
                    const dy = touch.clientY - touchStartPos.y;
                    if (Math.abs(dx) > DRAG_MOVE_THRESHOLD || Math.abs(dy) > DRAG_MOVE_THRESHOLD) {
                        isTouchMoved = true;
                        // 移动了 → 取消右键定时器
                        clearTimeout(longPressTimer);
                        // 移动超过阈值 → 进入拖拽
                        enterDragMode(touchStartPos.x, touchStartPos.y);
                        // 立即发一次 move 到当前位置
                        const pos = mapToScreen(touch.clientX, touch.clientY);
                        sendAction({ action: 'drag_move', x: pos.x, y: pos.y });
                        updateDragIndicator(touch.clientX, touch.clientY);
                    }
                }
            }
            // 双指滚动
            if (e.touches.length === 2) {
                clearTimeout(longPressTimer);
                // 双指时结束拖拽（如果正在拖拽）
                if (isDragging) {
                    exitDragMode(touch.clientX, touch.clientY);
                }
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

        // 滚轮模式
        if (state.scrollMode) {
            scrollAccumY = 0;
            scrollAccumX = 0;
            hideScrollIndicator();
            // 单击（未滑动）→ 退出滚轮模式，进入操作模式，发送点击
            if (!isTouchMoved && touchStartPos) {
                state.scrollMode = false;
                updateScrollModeUI();
                state.mode = 'control';
                updateModeUI();
                if (typeof saveClientState === 'function') saveClientState();
                const pos = mapToScreen(touchStartPos.x, touchStartPos.y);
                sendAction({ action: 'click', x: pos.x, y: pos.y });
                showRipple(touchStartPos.x, touchStartPos.y);
            }
            return;
        }

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

                // 双击还原/放大，单击切换到操作模式并发送点击
                if (!isTouchMoved) {
                    const now = Date.now();
                    const elapsed = now - touchStartTime;
                    if (elapsed < 300 && now - lastTapTime < 300) {
                        // 双击：缩放还原/放大
                        clearTimeout(_browseSingleTapTimer);
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
                        // 可能是单击，延迟 300ms 确认不是双击后切换到操作模式
                        lastTapTime = now;
                        const tapPos = { x: touchStartPos.x, y: touchStartPos.y };
                        clearTimeout(_browseSingleTapTimer);
                        _browseSingleTapTimer = setTimeout(() => {
                            // 确认是单击 → 切换到操作模式并发送点击
                            state.mode = 'control';
                            updateModeUI();
                            if (typeof saveClientState === 'function') saveClientState();
                            const pos = mapToScreen(tapPos.x, tapPos.y);
                            sendAction({ action: 'click', x: pos.x, y: pos.y });
                            showRipple(tapPos.x, tapPos.y);
                        }, 300);
                    }
                }

                // 保存当前缩放位置到窗口缓存（在所有 view 修改之后）
                if (typeof saveCurrentViewToCache === 'function') {
                    saveCurrentViewToCache();
                }
            }
            return;
        }

        // 操作模式
        if (isDragging) {
            // 拖拽释放
            const lastTouch = e.changedTouches[0];
            exitDragMode(lastTouch.clientX, lastTouch.clientY);
            showRipple(lastTouch.clientX, lastTouch.clientY, '#007aff');
            return;
        }

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
