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
# 模板字号 -> 标题层级
_TEMPLATE_SIZE_TO_LEVEL = {16: 1, 15: 2, 14: 3, 12: 4}

# 句末标点（完整句子的标志，标题一般不带）
_SENTENCE_END_CHARS = ('。', '！', '？', '；')


def infer_heading_level(elem: ContentElement, body_size: float) -> Optional[int]:
    """推断标题层级（1-4），非标题返回None

    信号优先级（从强到弱）：
    1. 字号显著大于正文 -> 按模板字号映射层级（16/15/14/12 -> 1/2/3/4）
       数字版PDF提取的字号精确可靠，是最强信号
    2. 文本模式匹配（"第X章"等强模式直接采信；
       "N." / "N.M" 弱模式需加粗或字号大于正文佐证，避免把
       "3.2 本次招标不接受联合体投标。"这类正文句子误判为标题）
    3. 加粗且字号略大于正文 -> 四级标题

    目录条目（is_toc）一律不是标题。
    """
    text = (elem.text or '').strip()
    if not text:
        return None
    if getattr(elem, 'is_toc', False):
        return None
    # 标题通常不会超过 100 字符
    if len(text) > 100:
        return None

    font_size = elem.font_size or 0
    is_bold = bool(elem.is_bold)
    ends_sentence = text.endswith(_SENTENCE_END_CHARS)

    # ---- 信号1：字号显著大于正文 -> 模板字号映射 ----
    if font_size > 0 and body_size > 0 and font_size >= body_size + 1.5:
        mapped = map_font_size(font_size)
        level = _TEMPLATE_SIZE_TO_LEVEL.get(mapped)
        if level == 4:
            # 12pt仅比正文大1.5pt左右，字号证据弱：交由信号2/3
            # （模式匹配/加粗）裁决，避免"电话：020-xxx"等
            # 个别放大的正文行被误判为四级标题
            pass
        elif level:
            # 大字号但完整长句（如通知正文的强调段）不当标题
            if ends_sentence and len(text) > 30:
                return None
            return level
        elif font_size >= body_size * 1.5:
            return 1
        else:
            return 3

    # ---- 信号2：文本模式匹配 ----
    pattern_level = _match_heading_pattern(text)
    if pattern_level:
        if pattern_level in (1, 2):
            # 强模式："第X章"/"一、"等，短且非完整句即可
            if len(text) <= 40 and not (ends_sentence and len(text) > 20):
                return pattern_level
            return None
        # 弱模式："N. 标题" / "N.M 标题"：需加粗或字号大于正文佐证
        if ends_sentence:
            return None
        if is_bold or (font_size > 0 and body_size > 0 and font_size > body_size + 0.5):
            if len(text) <= 40:
                return pattern_level
        return None

    # ---- 信号3：加粗且字号略大于正文 ----
    if (is_bold and font_size > 0 and body_size > 0
            and font_size > body_size + 0.5 and len(text) <= 40):
        return 4

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
