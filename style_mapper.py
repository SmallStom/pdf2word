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
    """将识别到的磅值映射到模板标准字号

    在 [16, 15, 14, 12, 10.5] 中找最接近的值。
    """
    if pt_value is None or pt_value <= 0:
        return BODY_FONT['size_pt']
    return min(TEMPLATE_FONT_SIZES, key=lambda s: abs(s - pt_value))


# ============================================================
# 正文字号检测
# ============================================================
def detect_body_font_size(elements: List[ContentElement]) -> float:
    """检测正文字号（所有文本块中出现频率最高的字号）

    用于作为标题判断的基线：字号明显大于正文的文本块可能是标题。
    """
    sizes = []
    for elem in elements:
        if elem.type == 'text' and elem.font_size and elem.font_size > 0:
            # 将字号四舍五入到0.5精度，便于统计
            rounded = round(elem.font_size * 2) / 2
            sizes.append(rounded)

    if not sizes:
        return BODY_FONT['size_pt']  # 默认小四号

    # 取众数
    size_counter = Counter(sizes)
    body_size = size_counter.most_common(1)[0][0]
    return body_size


# ============================================================
# 标题层级推断
# ============================================================
def infer_heading_level(elem: ContentElement, body_size: float) -> Optional[int]:
    """推断标题层级（1-4），非标题返回None

    信号优先级：
    1. 文本模式匹配（正则）- 最可靠
    2. 字号判断（原生PDF有字号信息时）
    3. 加粗+字号大于正文
    """
    text = elem.text.strip()
    if not text:
        return None

    # 过滤过长的文本（标题通常不超过80字符）
    if len(text) > 80:
        return None

    font_size = elem.font_size
    is_bold = elem.is_bold

    # ---- 信号1：文本模式匹配 ----
    pattern_level = _match_heading_pattern(text)

    # ---- 信号2：字号判断（原生PDF） ----
    size_level = None
    if font_size and font_size > 0:
        if font_size >= 16:
            size_level = 1
        elif font_size >= 15:
            size_level = 2
        elif font_size >= 14:
            size_level = 3
        elif font_size >= 13 and (is_bold or (body_size > 0 and font_size > body_size * 1.05)):
            size_level = 4

    # ---- 信号3：加粗+字号大于正文 ----
    bold_level = None
    if is_bold and body_size > 0 and font_size:
        ratio = font_size / body_size
        if ratio >= 1.3:
            bold_level = 1
        elif ratio >= 1.15:
            bold_level = 2
        elif ratio >= 1.05:
            bold_level = 3
        else:
            bold_level = 4  # 加粗但字号相近

    # ---- 融合决策 ----
    # 优先使用文本模式（最可靠）
    if pattern_level:
        # 如果同时有字号信息且字号支持该判断，直接采用
        if size_level and size_level != pattern_level:
            # 模式和字号冲突时，取较高级别（较小的数字）
            return min(pattern_level, size_level)
        return pattern_level

    # 无模式匹配时，用字号判断
    if size_level:
        return size_level

    # 最后用加粗判断
    if bold_level:
        return bold_level

    return None


def _match_heading_pattern(text: str) -> Optional[int]:
    """匹配中文标题编号模式"""
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
    """将中文数字转为阿拉伯数字

    例：一->1, 十->10, 十一->11, 二十三->23, 一百零五->105
    """
    if cn_str.isdigit():
        return int(cn_str)

    result = 0
    current = 0
    for char in cn_str:
        if char not in _CN_NUM_MAP:
            continue
        val = _CN_NUM_MAP[char]
        if val >= 10:  # 十/百/千
            if current == 0:
                current = 1
            result += current * val
            current = 0
        else:
            current = val
    result += current
    return result if result > 0 else 1


# ============================================================
# 章节号提取
# ============================================================
def extract_chapter_number(heading_text: str) -> int:
    """从一级标题文本中提取章节号

    支持：第X章、第X部分、第X编、第X篇、数字开头
    """
    text = heading_text.strip()

    # 第X章/部分/编/篇
    m = re.match(r'^第([零一二三四五六七八九十百千0-9]+)[章部分编篇]', text)
    if m:
        return chinese_to_int(m.group(1))

    # 纯数字开头：如 "1 概述" 或 "1.概述"
    m = re.match(r'^(\d+)[\s.．、]', text)
    if m:
        return int(m.group(1))

    return 0  # 无法提取


# ============================================================
# 图表编号管理器
# ============================================================
class FigureTableCounter:
    """跟踪当前章节，自动分章编号图片和表格

    生成编号格式：图X-Y、表X-Y（X=章节号，Y=章内序号）
    若无章节号则使用顺序编号：图1、图2...
    """

    def __init__(self):
        self._current_chapter = 0  # 当前章节号（0表示未进入任何章节）
        self._fig_count = 0        # 当前章节的图片序号
        self._tbl_count = 0        # 当前章节的表格序号
        self._global_fig = 0       # 全局图片序号（无章节号时使用）
        self._global_tbl = 0       # 全局表格序号（无章节号时使用）

    def update_chapter(self, heading_text: str, heading_level: int):
        """遇到标题时更新当前章节号"""
        if heading_level == 1:
            chapter_num = extract_chapter_number(heading_text)
            if chapter_num > 0:
                self._current_chapter = chapter_num
                self._fig_count = 0
                self._tbl_count = 0
            else:
                # 无法提取章节号，使用自增
                self._current_chapter += 1
                self._fig_count = 0
                self._tbl_count = 0

    def next_figure_number(self) -> str:
        """获取下一个图片编号"""
        if self._current_chapter > 0:
            self._fig_count += 1
            return f"图{self._current_chapter}-{self._fig_count}"
        else:
            self._global_fig += 1
            return f"图{self._global_fig}"

    def next_table_number(self) -> str:
        """获取下一个表格编号"""
        if self._current_chapter > 0:
            self._tbl_count += 1
            return f"表{self._current_chapter}-{self._tbl_count}"
        else:
            self._global_tbl += 1
            return f"表{self._global_tbl}"
