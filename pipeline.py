# -*- coding: utf-8 -*-
"""PDF转Word主流程编排模块

流程：版面分析(PaddleOCR) → 样式聚合 → 标题层级推断 → Word生成
"""

import os
import logging
from typing import List

from pdf_extractor import (
    ContentElement, extract_with_layout_analysis,
)
from style_mapper import (
    detect_body_font_size, infer_heading_level,
    map_font_size,
)
from word_generator import generate_word

logger = logging.getLogger(__name__)


class PDFToWordPipeline:
    """PDF转规范Word完整流水线"""

    def process(self, pdf_path: str, output_path: str) -> str:
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        # 1. 版面分析（PP-StructureV3 统一处理原生/扫描件）
        logger.info("开始 PP-StructureV3 版面分析...")
        elements = extract_with_layout_analysis(pdf_path, dpi=200)
        logger.info(f"提取到 {len(elements)} 个内容元素")

        if not elements:
            raise ValueError("PDF内容为空，无法转换")

        # 2. 检测正文字号基线
        body_size = detect_body_font_size(elements)
        logger.info(f"检测到正文字号: {body_size}pt")

        # 3. 标题层级推断 + 样式映射
        heading_count = 0
        for elem in elements:
            if elem.type in ('heading', 'text'):
                level = infer_heading_level(elem, body_size)
                if level:
                    elem.type = 'heading'
                    elem.heading_level = level
                    heading_count += 1
                else:
                    # 字号映射到模板标准字号
                    elem.mapped_size = map_font_size(
                        elem.font_size if elem.font_size else body_size
                    )

        logger.info(f"识别到 {heading_count} 个标题")

        # 4. 生成Word文档
        logger.info("开始生成Word文档...")
        generate_word(elements, output_path)
        logger.info(f"Word文档已生成: {output_path}")

        return output_path
