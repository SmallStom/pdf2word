# -*- coding: utf-8 -*-
"""格式规范配置 - 集中管理所有格式参数"""

import os

# ============================================================
# 页面设置
# ============================================================
PAGE_SIZE = {
    'width_cm': 21.0,    # A4宽度
    'height_cm': 29.7,   # A4高度
}

PAGE_MARGINS = {
    'top_cm': 2.54,
    'bottom_cm': 2.54,
    'left_cm': 3.18,
    'right_cm': 3.18,
}

HEADER_FOOTER_DISTANCE = {
    'header_cm': 1.5,
    'footer_cm': 1.75,
}

# 页面可用宽度（A4宽度 - 左右边距）
USABLE_WIDTH_CM = PAGE_SIZE['width_cm'] - PAGE_MARGINS['left_cm'] - PAGE_MARGINS['right_cm']  # 14.64cm

# ============================================================
# 字号映射表（中文字号 <-> 磅值）
# ============================================================
FONT_SIZE_MAP = {
    '三号': 16,
    '小三号': 15,
    '四号': 14,
    '小四号': 12,
    '五号': 10.5,
}

# 模板标准字号列表（磅值），用于字号映射
TEMPLATE_FONT_SIZES = [16, 15, 14, 12, 10.5]

# ============================================================
# 标题配置
# ============================================================
HEADING_SIZES = {
    1: 16,   # 三号 - 一级标题
    2: 15,   # 小三号 - 二级标题
    3: 14,   # 四号 - 三级标题
    4: 12,   # 小四号 - 四级及以上
}

HEADING_FONT = {
    'cn': '宋体',
    'en': 'Times New Roman',
    'bold': True,
}

# 中文标题编号正则模式（按优先级排序）
# 兼容性写法：半角全角点号、空格、可选中英文数字混用
import re

HEADING_PATTERNS = [
    # 一级标题：第X章/部分/编/篇（兼容"第一章"/"第1章"/"第 1 章"/"第1 章"）
    (1, re.compile(r'^第\s*[零一二三四五六七八九十百千0-9]+\s*[章部分编篇]\s*.+')),
    (1, re.compile(r'^Chapter\s*\d+\s*.+', re.IGNORECASE)),
    # 二级标题：第X节/条/款
    (2, re.compile(r'^第\s*[零一二三四五六七八九十百千0-9]+\s*[节条款]\s*.+')),
    (2, re.compile(r'^\d+[．.、]\d+\s+.+')),         # "1.1 概述" / "1．1 概述"
    (2, re.compile(r'^\d+[、]\s*.+')),                # "1、概述"
    (2, re.compile(r'^\d+[．\.][^0-9]')),             # "1. 招标条件" / "1．招标条件"
    # 三级标题
    (3, re.compile(r'^\d+[．.]\d+[．.]\d+\s*.+')),    # "1.1.1 研究背景"
    (3, re.compile(r'^[（(][一二三四五六七八九十0-9]+[)）]\s*.+')),  # "(一) 测试"
    # 四级标题
    (4, re.compile(r'^\d+[．.]\d+[．.]\d+[．.]\d+\s*.+')),  # "1.1.1.1"
    (4, re.compile(r'^[（(]\d+[)）]\s*.+')),           # "(1) 测试"
]

# ============================================================
# 正文配置
# ============================================================
BODY_FONT = {
    'cn': '宋体',
    'en': 'Times New Roman',
    'size_pt': 12,          # 小四号
    'bold': False,
    'line_spacing': 1.5,    # 1.5倍行距
    'space_before_pt': 0,
    'space_after_pt': 0,
    'first_line_indent_chars': 2,  # 首行缩进2字符
    'snap_to_grid': False,         # 取消网格对齐
}

# ============================================================
# 表格配置
# ============================================================
TABLE_CONFIG = {
    'row_height_cm': 0.75,       # 行高0.7-0.8cm，取中间值
    'row_height_rule': 'atLeast', # 至少0.75cm，内容多时自动扩展
    'cell_font_cn': '宋体',
    'cell_font_en': 'Times New Roman',
    'cell_font_size_pt': 10.5,   # 五号
    'cell_line_spacing': 1.0,    # 单倍行距
    'cell_space_before_pt': 0,
    'cell_space_after_pt': 0,
    # 表名配置
    'name_font_cn': '宋体',
    'name_font_en': 'Times New Roman',
    'name_font_size_pt': 12,     # 小四号
    'name_bold': True,
    'name_alignment': 'center',
    'repeat_header': True,       # 重复标题行
    'cant_split_row': True,      # 行不跨页拆分
}

# ============================================================
# 图片配置
# ============================================================
IMAGE_CONFIG = {
    'alignment': 'center',       # 居中
    'inline': True,              # 嵌入型
    'max_width_cm': USABLE_WIDTH_CM,  # 最大宽度=页面可用宽度
    # 图名配置
    'name_font_cn': '宋体',
    'name_font_en': 'Times New Roman',
    'name_font_size_pt': 12,     # 小四号
    'name_bold': False,
    'name_alignment': 'center',
    'name_position': 'below',    # 图名位于图下方
}

# ============================================================
# PDF类型检测配置
# ============================================================
PDF_TYPE_DETECTION = {
    'scanned_threshold': 100,   # 每页平均文本字符数阈值，低于此值判定为扫描件
    'ocr_dpi': 200,             # 扫描件OCR的DPI
}

# 扫描件字号估算系数
OCR_FONT_SIZE_FACTOR = 0.8  # font_size ≈ bbox_height * 72 / dpi * factor

# ============================================================
# PaddleOCR 运行设备配置
# ============================================================
# 'cpu' 或 'gpu'
# CPU模式：安装 paddlepaddle，内存占用约1.5-2GB
# GPU模式：安装 paddlepaddle-gpu，显存占用约1-2GB，推理速度提升5-10倍
# 支持环境变量 PADDLE_DEVICE 覆盖（Docker中使用）
PADDLE_DEVICE = os.environ.get('PADDLE_DEVICE', 'gpu')

# GPU卡号选择（0, 1, 2, 3...）
# 指定使用哪张显卡，多卡环境下必配
# 也可通过 CUDA_VISIBLE_DEVICES 环境变量控制（Docker推荐用后者）
PADDLE_GPU_ID = int(os.environ.get('PADDLE_GPU_ID', '0'))

# GPU显存限制（GB）
# 显卡共享时设置此项，限制PaddleOCR最大使用的显存
# 设为 None 则不限制（使用全部可用显存）
# 支持环境变量 PADDLE_GPU_MEMORY_GB 覆盖
PADDLE_GPU_MEMORY_GB = float(os.environ.get('PADDLE_GPU_MEMORY_GB', '4')) if os.environ.get('PADDLE_GPU_MEMORY_GB') else 4
