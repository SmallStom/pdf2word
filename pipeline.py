# -*- coding: utf-8 -*-
"""PDF转Word主流程编排模块

流程：PDF类型检测 -> 内容提取 -> 样式映射 -> 标题推断 -> Word生成
"""

import os
import logging
from typing import List

from pdf_extractor import (
    ContentElement, detect_pdf_type,
    extract_native_pdf, extract_scanned_pdf,
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
        """完整处理流程

        Args:
            pdf_path: 输入PDF文件路径
            output_path: 输出Word文件路径

        Returns:
            输出文件路径
        """
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        # 1. 检测PDF类型
        pdf_type = detect_pdf_type(pdf_path)
        logger.info(f"PDF类型检测: {pdf_type}")

        # 2. 提取内容
        if pdf_type == 'native':
            logger.info("使用PyMuPDF提取原生PDF内容...")
            elements = extract_native_pdf(pdf_path)
        else:
            logger.info("使用PaddleOCR提取扫描件内容...")
            elements = extract_scanned_pdf(pdf_path)

        logger.info(f"提取到 {len(elements)} 个内容元素")

        if not elements:
            raise ValueError("PDF内容为空，无法转换")

        # 3. 检测正文字号基线
        body_size = detect_body_font_size(elements)
        logger.info(f"检测到正文字号: {body_size}pt")

        # 4. 标题层级推断 + 样式映射
        heading_count = 0
        for elem in elements:
            if elem.type == 'text':
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

        # 5. 生成Word文档
        logger.info("开始生成Word文档...")
        generate_word(elements, output_path)
        logger.info(f"Word文档已生成: {output_path}")

        return output_path
