"""增量传输 DeltaEncoder 测试 + 带宽对比"""
import json
import time
from PIL import Image
import numpy as np

from server.core.screen import DeltaEncoder


def test_basic():
    """基础功能测试"""
    de = DeltaEncoder(quality=50)
    print(f"DeltaEncoder: block_size={de.BLOCK_SIZE}, keyframe={de.KEYFRAME_INTERVAL}")

    # Frame 1: 首帧 → full
    img1 = Image.fromarray(np.zeros((800, 1280, 3), dtype=np.uint8))
    t1, p1 = de.encode(img1)
    assert t1 == "full", f"Expected full, got {t1}"
    print(f"  Frame 1: type={t1}, size={len(p1)} bytes")

    # Frame 2: 相同 → skip
    img2 = Image.fromarray(np.zeros((800, 1280, 3), dtype=np.uint8))
    t2, p2 = de.encode(img2)
    assert t2 == "skip", f"Expected skip, got {t2}"
    print(f"  Frame 2: type={t2}")

    # Frame 3: 小区域变化 → delta
    arr3 = np.zeros((800, 1280, 3), dtype=np.uint8)
    arr3[100:200, 100:300] = 255
    img3 = Image.fromarray(arr3)
    t3, p3 = de.encode(img3)
    assert t3 == "delta", f"Expected delta, got {t3}"
    data3 = json.loads(p3)
    print(f"  Frame 3: type={t3}, regions={len(data3['regions'])}, size={len(p3)} bytes")

    # Frame 4: 大面积变化 → full
    arr4 = np.ones((800, 1280, 3), dtype=np.uint8) * 128
    img4 = Image.fromarray(arr4)
    t4, p4 = de.encode(img4)
    assert t4 == "full", f"Expected full, got {t4}"
    print(f"  Frame 4: type={t4}, size={len(p4)} bytes")

    print("  ✅ 基础测试通过\n")


def test_bandwidth_comparison():
    """模拟真实场景：对比增量传输 vs 完整帧的带宽"""
    de = DeltaEncoder(quality=50)
    
    frames = 100
    full_total = 0    # 完整帧模式的总字节数
    delta_total = 0   # 增量传输的总字节数
    stats = {"full": 0, "delta": 0, "skip": 0}

    # 模拟场景：大部分时间屏幕静止，偶尔有小区域变化
    np.random.seed(42)
    base = np.random.randint(50, 200, (800, 1280, 3), dtype=np.uint8)

    start = time.time()
    for i in range(frames):
        arr = base.copy()
        
        if i % 10 == 0:
            # 每10帧有小区域变化(模拟光标移动/文本输入)
            y, x = np.random.randint(0, 700), np.random.randint(0, 1200)
            arr[y:y+50, x:x+80] = np.random.randint(0, 255, (50, 80, 3), dtype=np.uint8)
        elif i % 30 == 5:
            # 偶尔有大变化(模拟切换窗口)
            arr = np.random.randint(0, 255, (800, 1280, 3), dtype=np.uint8)

        img = Image.fromarray(arr)
        
        # 完整帧大小
        import io
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50, optimize=True)
        full_size = len(buf.getvalue())
        full_total += full_size

        # 增量编码
        t, p = de.encode(img)
        stats[t] += 1
        if t == "full":
            delta_total += len(p)
        elif t == "delta":
            delta_total += len(p)
        # skip: 0 bytes

    elapsed = time.time() - start

    print(f"  场景: {frames} 帧模拟 (大部分静止+偶尔小变化)")
    print(f"  编码耗时: {elapsed:.2f}s ({frames/elapsed:.0f} fps)")
    print(f"  帧统计: full={stats['full']}, delta={stats['delta']}, skip={stats['skip']}")
    print(f"  完整帧模式总带宽: {full_total/1024:.0f} KB ({full_total/1024/frames:.1f} KB/帧)")
    print(f"  增量传输总带宽:   {delta_total/1024:.0f} KB ({delta_total/1024/frames:.1f} KB/帧)")
    savings = (1 - delta_total / full_total) * 100 if full_total > 0 else 0
    print(f"  🎯 带宽节省: {savings:.1f}%")
    print(f"  ✅ 带宽对比测试完成\n")


def test_keyframe():
    """关键帧间隔测试"""
    de = DeltaEncoder(quality=50)
    
    # 发送 60 帧相同图像
    img = Image.fromarray(np.zeros((800, 1280, 3), dtype=np.uint8))
    
    types = []
    for i in range(65):
        t, _ = de.encode(img)
        types.append(t)
    
    # 第1帧是 full (首帧)
    assert types[0] == "full"
    # 第60帧是 full (keyframe)
    assert types[59] == "full"
    # 其余都是 skip
    skip_count = types.count("skip")
    full_count = types.count("full")
    print(f"  65 帧: full={full_count}, skip={skip_count}")
    assert full_count == 2  # 第1帧 + 第60帧
    print("  ✅ 关键帧间隔测试通过\n")


if __name__ == "__main__":
    print("=" * 50)
    print("DeltaEncoder 测试套件")
    print("=" * 50)
    
    print("\n1. 基础功能测试")
    test_basic()
    
    print("2. 关键帧间隔测试")
    test_keyframe()
    
    print("3. 带宽节省对比测试")
    test_bandwidth_comparison()
    
    print("=" * 50)
    print("✅ 全部测试通过!")
