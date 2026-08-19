# -*- coding: utf-8 -*-
"""样式映射 & 标题层级推断模块"""

import re
from typing import List, Optional
from collections import Counter

from config import (
    TEMPLATE_FONT_SIZES, HEADING_SIZES, HEADING_PATTERNS,
    BODY_FONT,
)
from pdf_extractor import ContentElement


# ============================================================
# 字号映射
# ============================================================
def map_font_size(pt_value: float) -> float:
    """将识别到的磅值映射到模板标准字号"""
    if pt_value is None or pt_value <= 0:
        return BODY_FONT['size_pt']
    return min(TEMPLATE_FONT_SIZES, key=lambda s: abs(s - pt_value))


# ============================================================
# 正文字号检测
# ============================================================
def detect_body_font_size(elements: List[ContentElement]) -> float:
    """检测正文字号（所有文本块中出现频率最高的字号）"""
    sizes = []
    for elem in elements:
        if elem.type == 'text' and elem.font_size and elem.font_size > 0:
            rounded = round(elem.font_size * 2) / 2
            sizes.append(rounded)

    if not sizes:
        return BODY_FONT['size_pt']

    size_counter = Counter(sizes)
    return size_counter.most_common(1)[0][0]


# ============================================================
# 标题层级推断
# ============================================================
def infer_heading_level(elem: ContentElement, body_size: float) -> Optional[int]:
    """推断标题层级（1-4），非标题返回None

    信号优先级（从强到弱）：
    1. 文本模式匹配（中文标题编号正则）—— 最可靠
    2. 元素 type 已经是 'heading'（来自 PP-StructureV3 版面分析）
    3. 字号判断（>=16pt 必为标题）
    4. 加粗+字号大于正文
    """
    text = elem.text.strip()
    if not text:
        return None

    # 标题通常不会超过 100 字符
    if len(text) > 100:
        return None

    font_size = elem.font_size
    is_bold = elem.is_bold
    is_layout_heading = elem.type == 'heading'

    # ---- 信号1：文本模式匹配（最可靠，尤其一级"第X章"）----
    pattern_level = _match_heading_pattern(text)
    if pattern_level:
        return pattern_level

    # ---- 以下无文本模式，用字号保守判断 ----
    # 关键：版面"heading"不再默认标为一级，避免普通小节标题被误判为
    # 一级标题而另起一页。是否一级只看字号是否显著大于正文。
    if not font_size or font_size <= 0:
        # 无字号信息（扫描件或缺span）：仅当版面明确判为标题时给中等层级，
        # 避免漏掉真实标题，但也绝不默认成一级
        return 3 if is_layout_heading else None

    if body_size and body_size > 0 and font_size <= body_size + 0.5:
        # 字号与正文相当 → 大概率是加粗正文/列表大项，不是标题
        return None

    scale = font_size / body_size if body_size and body_size > 0 else 1.0
    # 一级标题需要非常显著的证据（大字号，或明显大于正文倍数）
    if is_bold:
        if font_size >= 18 or scale >= 1.5:
            return 1
        if font_size >= 16 or scale >= 1.25:
            return 2
        if font_size >= 14 or scale >= 1.1:
            return 3
        return None
    # 非加粗：只有明显大于正文才算标题
    if scale >= 1.4:
        return 1
    if scale >= 1.2:
        return 2
    return None


def _match_heading_pattern(text: str) -> Optional[int]:
    """匹配中文标题编号模式（宽松版）"""
    text = text.strip()
    for level, pattern in HEADING_PATTERNS:
        if pattern.match(text):
            return level
    return None


# ============================================================
# 中文字符转数字
# ============================================================
_CN_NUM_MAP = {
    '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
    '十': 10, '百': 100, '千': 1000,
}


def chinese_to_int(cn_str: str) -> int:
    """将中文数字转为阿拉伯数字"""
    if cn_str.isdigit():
        return int(cn_str)
    result = 0
    current = 0
    for char in cn_str:
        if char not in _CN_NUM_MAP:
            continue
        val = _CN_NUM_MAP[char]
        if val >= 10:
            if current == 0:
                current = 1
            result += current * val
            current = 0
        else:
            current = val
    result += current
    return result if result > 0 else 1


def extract_chapter_number(heading_text: str) -> int:
    """从一级标题文本中提取章节号"""
    text = heading_text.strip()

    # 第X章/部分/编/篇（兼容"第1章"、"第一章"、"第 1 章"）
    m = re.match(r'^第\s*([零一二三四五六七八九十百千0-9]+)\s*[章部分编篇]', text)
    if m:
        return chinese_to_int(m.group(1).replace(' ', ''))

    # 纯数字开头："1 概述"、"1.概述"、"1、概述"
    m = re.match(r'^(\d+)\s*[.．、\s]', text)
    if m:
        return int(m.group(1))

    return 0


# ============================================================
# 图表编号管理器
# ============================================================
class FigureTableCounter:
    """跟踪当前章节，自动分章编号图片和表格"""

    def __init__(self):
        self._current_chapter = 0
        self._fig_count = 0
        self._tbl_count = 0
        self._global_fig = 0
        self._global_tbl = 0

    def update_chapter(self, heading_text: str, heading_level: int):
        if heading_level == 1:
            chapter_num = extract_chapter_number(heading_text)
            if chapter_num > 0:
                self._current_chapter = chapter_num
                self._fig_count = 0
                self._tbl_count = 0
            else:
                self._current_chapter += 1
                self._fig_count = 0
                self._tbl_count = 0

    def next_figure_number(self) -> str:
        if self._current_chapter > 0:
            self._fig_count += 1
            return f"图{self._current_chapter}-{self._fig_count}"
        else:
            self._global_fig += 1
            return f"图{self._global_fig}"

    def next_table_number(self) -> str:
        if self._current_chapter > 0:
            self._tbl_count += 1
            return f"表{self._current_chapter}-{self._tbl_count}"
        else:
            self._global_tbl += 1
            return f"表{self._global_tbl}"
