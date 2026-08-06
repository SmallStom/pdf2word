#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预下载PaddleOCR模型

在Docker构建阶段运行，将PaddleOCR所需的OCR识别、版面分析、表格识别等模型
预下载到镜像中，避免容器启动后首次使用时下载缓慢。
"""

import sys
import os

# 设置模型存储路径
os.environ.setdefault('PADDLE_OCR_HOME', '/opt/paddleocr')

print("=" * 60)
print("  PaddleOCR 模型预下载")
print("=" * 60)

try:
    from paddleocr import PPStructureV3 as Engine
    print("使用 PPStructureV3 引擎")
except ImportError:
    try:
        from paddleocr import PPStructure as Engine
        print("使用 PPStructure 引擎（旧版兼容）")
    except ImportError:
        print("[SKIP] PaddleOCR 未安装，跳过模型下载")
        sys.exit(0)

try:
    print("\n正在初始化引擎并下载模型（可能需要几分钟）...")
    print("模型来源: paddleocr.bj.bcebos.com（百度云，国内直连）\n")

    engine = Engine(
        show_log=True,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_seal_recognition=False,
        use_chart_recognition=False,
        use_formula_recognition=True,
        use_table_recognition=True,
    )

    print("\n" + "=" * 60)
    print("  PaddleOCR 模型下载完成!")
    print("=" * 60)

except Exception as e:
    print(f"\n[WARNING] 模型预下载失败: {e}")
    print("模型将在容器首次使用时自动下载。")
    sys.exit(0)  # 不中断构建
