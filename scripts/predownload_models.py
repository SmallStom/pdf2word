#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预下载PaddleOCR/PaddleX模型

在Docker构建阶段运行，将PPStructureV3所需的模型预下载到镜像层（~/.paddlex/official_models/）。
"""

import sys
import os
import shutil

print("=" * 60)
print("  PaddleX 模型预下载")
print("=" * 60)

# 1. 设置环境变量
os.environ.setdefault('PADDLE_OCR_HOME', '/opt/paddleocr')
# HF 镜像源（仅在需要下载 HuggingFace 模型时生效）
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')

# 2. 验证 PaddleOCR 可用
try:
    from paddleocr import PPStructureV3
    print("使用 PPStructureV3 引擎")
except ImportError as e:
    print(f"[SKIP] PaddleOCR 未安装: {e}")
    sys.exit(0)

# 3. 显示模型缓存位置
cache_dir = os.path.expanduser('~/.paddlex/official_models')
print(f"模型缓存目录: {cache_dir}")
if os.path.exists(cache_dir):
    pre_size = sum(
        os.path.getsize(os.path.join(d, f))
        for d, _, fs in os.walk(cache_dir) for f in fs
    )
    print(f"  已存在模型: {pre_size / 1024 / 1024:.1f} MB")
else:
    print("  尚未下载")

# 4. 初始化引擎（这一步会触发模型下载）
try:
    print("\n正在初始化 PPStructureV3 并下载模型（约 1-2GB）...")
    print("模型源: 国内 paddleocr.bj.bcebos.com\n")

    engine = PPStructureV3(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_formula_recognition=False,
        use_table_recognition=True,
        use_chart_recognition=False,
        use_region_detection=True,
        lang='ch',
        device='cpu',  # 构建时用 CPU 下载（避免无 GPU 报错）
    )

    print("\n" + "=" * 60)
    print("  引擎初始化完成")
    print("=" * 60)

    # 5. 在第一张图上跑一次以触发所有模型下载
    try:
        from PIL import Image
        import numpy as np
        fake_img = np.zeros((640, 480, 3), dtype=np.uint8)
        fake_path = '/tmp/_fake_for_download.png'
        Image.fromarray(fake_img).save(fake_path)
        print(f"\n在伪图上跑一次 predict() 触发所有模型下载...")
        list(engine.predict(fake_path))
        os.remove(fake_path)
        print("模型预下载完成")
    except Exception as e:
        print(f"[WARNING] predict 触发下载失败: {e}")
        print("部分模型可能未下载，运行时再补齐")

    # 6. 打印已下载模型大小
    if os.path.exists(cache_dir):
        post_size = sum(
            os.path.getsize(os.path.join(d, f))
            for d, _, fs in os.walk(cache_dir) for f in fs
        )
        print(f"\n当前模型缓存: {post_size / 1024 / 1024:.1f} MB")
        models = sorted(os.listdir(cache_dir))
        print(f"已下载模型: {', '.join(models)}")

except Exception as e:
    print(f"\n[WARNING] 模型预下载失败: {e}")
    import traceback
    traceback.print_exc()
    print("模型将在容器首次使用时自动下载。")
    sys.exit(0)  # 不中断构建
