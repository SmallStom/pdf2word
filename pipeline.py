# -*- coding: utf-8 -*-
"""PDF转Word主流程编排模块

流程：版面分析(PaddleOCR) → 样式聚合 → 标题层级推断 → Word生成
"""

import os
import logging
from typing import List

from pdf_extractor import (
    ContentElement, extract_with_layout_analysis,
    extract_digital_pdf, is_digital_pdf,
)
from style_mapper import (
    detect_body_font_size, infer_heading_level,
)
from word_generator import generate_word
from config import BODY_FONT

logger = logging.getLogger(__name__)


class PDFToWordPipeline:
    """PDF转规范Word完整流水线"""

    def process(self, pdf_path: str, output_path: str) -> str:
        print(f"[STAGE 0] process() entered, pdf={pdf_path}", flush=True)
        if not os.path.exists(pdf_path):
            print(f"[STAGE 0] PDF file not found!", flush=True)
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        # 1. 提取：数字版PDF走纯PyMuPDF精确提取（无OCR误差、保字符完整），
        #    扫描件走 PP-StructureV3 版面分析
        digital = is_digital_pdf(pdf_path)
        if digital:
            print(f"[STAGE 1] 数字版PDF -> PyMuPDF精确提取", flush=True)
            logger.info("数字版PDF，使用PyMuPDF精确提取...")
            elements = extract_digital_pdf(pdf_path)
        else:
            print(f"[STAGE 1] 扫描版PDF -> PP-StructureV3版面分析", flush=True)
            logger.info("开始 PP-StructureV3 版面分析...")
            elements = extract_with_layout_analysis(pdf_path, dpi=200)
        print(f"[STAGE 1] returned with {len(elements)} elements", flush=True)
        logger.info(f"提取到 {len(elements)} 个内容元素")

        if not elements:
            raise ValueError("PDF内容为空，无法转换")

        # 0. 诊断：按 type 统计
        from collections import Counter
        type_counter = Counter(e.type for e in elements)
        print(f"[DIAG] 元素类型分布: {dict(type_counter)}", flush=True)

        # 2. 检测正文字号基线
        body_size = detect_body_font_size(elements)
        logger.info(f"检测到正文字号: {body_size}pt")

        # 3. 标题层级推断 + 样式映射
        #    目录条目(toc)不参与标题推断，保持制表位结构原样输出
        heading_count = 0
        for elem in elements:
            if elem.type == 'toc':
                continue
            if elem.type in ('heading', 'text'):
                level = infer_heading_level(elem, body_size)
                if level:
                    elem.type = 'heading'
                    elem.heading_level = level
                    heading_count += 1
                    # 固定格式：一级标题居中，二级及以下左对齐
                    if level == 1:
                        elem.alignment = 'center'
                    else:
                        elem.alignment = 'left'
                else:
                    # 正文统一使用小四号 12pt（固定格式规范）
                    elem.mapped_size = float(BODY_FONT['size_pt'])
                    # 行内run字号按映射比例缩放，保持行内相对大小
                    # （如上标较小/局部强调较大），并规避Word半磅精度截断
                    if elem.runs and elem.font_size and elem.font_size > 0:
                        factor = elem.mapped_size / elem.font_size
                        for rd in elem.runs:
                            if rd.get('size') and rd['size'] > 0:
                                rd['size'] = rd['size'] * factor
                    # 缩进按同比例换算到映射字号（PDF 10.4pt 的 2字符
                    # 缩进 -> 12pt 字号下约 2字符），并设上限防误检远端行
                    if elem.font_size and elem.font_size > 0:
                        factor = elem.mapped_size / elem.font_size
                        if elem.left_indent_pt:
                            elem.left_indent_pt = min(elem.left_indent_pt * factor, 300.0)
                        if elem.first_line_indent_pt is not None:
                            elem.first_line_indent_pt = elem.first_line_indent_pt * factor

        logger.info(f"识别到 {heading_count} 个标题")

        # 4. 生成Word文档
        logger.info("开始生成Word文档...")
        generate_word(elements, output_path)
        logger.info(f"Word文档已生成: {output_path}")

        return output_path
