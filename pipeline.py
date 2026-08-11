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
        import sys
        print(f"[STAGE 0] process() entered, pdf={pdf_path}", flush=True)
        if not os.path.exists(pdf_path):
            print(f"[STAGE 0] PDF file not found!", flush=True)
            raise FileNotFoundError(f"PDF文件不存在: {pdf_path}")

        print(f"[STAGE 1] calling extract_with_layout_analysis", flush=True)
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

        # 0.1 诊断：非空文本元素数量
        nonempty = sum(1 for e in elements if e.text and e.text.strip())
        empty_text = sum(1 for e in elements if e.type in ('text','heading') and (not e.text or not e.text.strip()))
        print(f"[DIAG] 非空文本: {nonempty}, 空文本: {empty_text}", flush=True)

        # 0.2 诊断：前 3 个非空元素的内容样本
        samples = [e for e in elements if e.text and e.text.strip()][:3]
        for i, e in enumerate(samples):
            preview = e.text[:80].replace('\n', ' ')
            print(f"[DIAG] 样本{i+1} type={e.type} text={preview!r}", flush=True)

        # 0.3 诊断：表格/图片数量
        tables_with_html = sum(1 for e in elements if e.type == 'table' and e.html)
        images_with_data = sum(1 for e in elements if e.type == 'image' and e.image_data)
        print(f"[DIAG] 表格(有html): {tables_with_html}, 图片(有data): {images_with_data}", flush=True)

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
