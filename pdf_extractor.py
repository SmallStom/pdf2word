# -*- coding: utf-8 -*-
"""PDF内容提取模块 - 支持原生PDF和扫描件双路径提取"""

import re
import io
from dataclasses import dataclass, field
from typing import List, Optional

import fitz  # PyMuPDF


# ============================================================
# 统一数据结构
# ============================================================
@dataclass
class ContentElement:
    """PDF内容元素的统一表示"""
    type: str  # 'text', 'heading', 'table', 'image'
    text: str = ''
    bbox: List[float] = field(default_factory=list)  # [x1, y1, x2, y2]
    page_num: int = 0

    # 样式信息（原生PDF有值，扫描件为None）
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    is_bold: Optional[bool] = None
    is_italic: Optional[bool] = None

    # 表格专用
    html: Optional[str] = None

    # 图片专用
    image_data: Optional[bytes] = None

    # 后处理填充
    heading_level: Optional[int] = None
    mapped_size: Optional[float] = None
    alignment: Optional[str] = None  # 'left', 'center', 'right'


# ============================================================
# PDF类型检测
# ============================================================
def detect_pdf_type(pdf_path: str) -> str:
    """检测PDF类型：'native'（原生）或 'scanned'（扫描件）

    判断依据：每页平均文本字符数，低于阈值则判定为扫描件。
    """
    from config import PDF_TYPE_DETECTION

    doc = fitz.open(pdf_path)
    total_chars = 0
    total_pages = len(doc)

    if total_pages == 0:
        return 'scanned'

    for page in doc:
        text = page.get_text("text")
        total_chars += len(text.strip())

    doc.close()

    avg_chars = total_chars / total_pages
    threshold = PDF_TYPE_DETECTION['scanned_threshold']
    return 'native' if avg_chars >= threshold else 'scanned'


# ============================================================
# 原生PDF提取（PyMuPDF + pdfplumber）
# ============================================================
def extract_native_pdf(pdf_path: str) -> List[ContentElement]:
    """使用PyMuPDF提取原生PDF内容

    提取内容：
    - 文本（含字体名、字号、粗体/斜体）
    - 图片（嵌入图片二进制数据）
    - 表格（通过pdfplumber提取，转为HTML）
    """
    import pdfplumber

    doc = fitz.open(pdf_path)
    elements: List[ContentElement] = []

    # 第一遍：用pdfplumber提取表格，记录表格区域
    table_regions_by_page = {}  # {page_num: [(bbox, html), ...]}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.find_tables()
            page_tables = []
            for table in tables:
                # 提取表格数据
                table_data = table.extract()
                if table_data and len(table_data) > 0:
                    html = _table_data_to_html(table_data)
                    # pdfplumber的bbox: (x1, top, x2, bottom)
                    bbox = [table.bbox[0], table.bbox[1],
                            table.bbox[2], table.bbox[3]]
                    page_tables.append((bbox, html))
            if page_tables:
                table_regions_by_page[page_num] = page_tables

    # 第二遍：用PyMuPDF提取文本和图片
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        page_width = page.rect.width
        page_rect = page.rect

        # 获取表格区域（用于过滤文本）
        table_regions = table_regions_by_page.get(page_num, [])

        # 提取文本块
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block["type"] != 0:  # 非文本块（图片由单独逻辑处理）
                continue

            block_bbox = block["bbox"]
            # 检查是否在表格区域内，如果是则跳过
            if _is_in_table_region(block_bbox, table_regions):
                continue

            # 合并block中所有line的span
            block_text = ""
            max_size = 0
            font_name = ""
            is_bold = False
            is_italic = False

            for line in block.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    span_text = span.get("text", "")
                    line_text += span_text

                    # 记录最大字号（代表该行的主字号）
                    span_size = span.get("size", 12)
                    if span_size > max_size:
                        max_size = span_size
                        font_name = span.get("font", "")

                    # 检查粗体/斜体
                    flags = span.get("flags", 0)
                    if flags & 16:  # bit 4 = bold
                        is_bold = True
                    if flags & 2:   # bit 1 = italic
                        is_italic = True

                if line_text.strip():
                    block_text += line_text + "\n"

            block_text = block_text.strip()
            if not block_text:
                continue

            # 检测对齐方式
            alignment = _detect_alignment(block_bbox, page_width)

            elements.append(ContentElement(
                type='text',
                text=block_text,
                bbox=list(block_bbox),
                page_num=page_num,
                font_name=font_name if font_name else None,
                font_size=max_size if max_size > 0 else None,
                is_bold=is_bold if is_bold else None,
                is_italic=is_italic if is_italic else None,
                alignment=alignment,
            ))

        # 提取图片
        image_list = page.get_images(full=True)
        for img_info in image_list:
            xref = img_info[0]
            try:
                img_data = doc.extract_image(xref)
                image_bytes = img_data["image"]
                # 获取图片在页面上的位置
                img_bboxes = page.get_image_rects(xref)
                if img_bboxes:
                    for img_rect in img_bboxes:
                        elements.append(ContentElement(
                            type='image',
                            text='',
                            bbox=[img_rect.x0, img_rect.y0,
                                  img_rect.x1, img_rect.y1],
                            page_num=page_num,
                            image_data=image_bytes,
                        ))
                else:
                    # 图片没有位置信息，放在页面末尾
                    elements.append(ContentElement(
                        type='image',
                        text='',
                        bbox=[0, page_rect.height * 0.5,
                              page_rect.width, page_rect.height * 0.5],
                        page_num=page_num,
                        image_data=image_bytes,
                    ))
            except Exception:
                continue

        # 添加表格元素
        for table_bbox, table_html in table_regions:
            elements.append(ContentElement(
                type='table',
                text='',
                bbox=table_bbox,
                page_num=page_num,
                html=table_html,
            ))

    doc.close()

    # 按页面和位置排序
    elements.sort(key=lambda e: (e.page_num, e.bbox[1] if e.bbox else 0,
                                  e.bbox[0] if e.bbox else 0))
    return elements


def _table_data_to_html(table_data: List[List[str]]) -> str:
    """将pdfplumber提取的表格数据转为HTML"""
    html = '<table>'
    for i, row in enumerate(table_data):
        html += '<tr>'
        tag = 'th' if i == 0 else 'td'
        for cell in row:
            cell_text = cell if cell else ''
            cell_text = cell_text.replace('<', '&lt;').replace('>', '&gt;')
            html += f'<{tag}>{cell_text}</{tag}>'
        html += '</tr>'
    html += '</table>'
    return html


def _is_in_table_region(bbox, table_regions, threshold=0.5) -> bool:
    """检查bbox是否在表格区域内（重叠面积超过50%则认为在表格内）"""
    x1, y1, x2, y2 = bbox
    area = (x2 - x1) * (y2 - y1)
    if area <= 0:
        return False

    for table_bbox, _ in table_regions:
        tx1, ty1, tx2, ty2 = table_bbox
        # 计算重叠区域
        ox1 = max(x1, tx1)
        oy1 = max(y1, ty1)
        ox2 = min(x2, tx2)
        oy2 = min(y2, ty2)
        if ox1 < ox2 and oy1 < oy2:
            overlap = (ox2 - ox1) * (oy2 - oy1)
            if overlap / area > threshold:
                return True
    return False


def _detect_alignment(bbox, page_width) -> str:
    """根据bbox位置检测对齐方式"""
    x1, y1, x2, y2 = bbox
    left = x1
    right = page_width - x2
    elem_width = x2 - x1

    # 居中：左右边距接近
    if abs(left - right) < 50 and elem_width < page_width * 0.8:
        return 'center'
    # 右对齐
    elif right < 50 and left > 100:
        return 'right'
    else:
        return 'left'


# ============================================================
# 扫描件提取（PaddleOCR PP-StructureV3）
# ============================================================
def extract_scanned_pdf(pdf_path: str) -> List[ContentElement]:
    """使用PaddleOCR PP-StructureV3提取扫描件内容

    流程：
    1. PyMuPDF将PDF每页转为图像
    2. PP-StructureV3进行版面分析+OCR
    3. 识别区域类型：text/title/table/figure
    4. 表格区域输出HTML
    5. 图片区域裁剪保存

    注意：OCR无法获取字体名/字号/粗体信息，用bbox高度估算字号。
    """
    from config import PDF_TYPE_DETECTION, OCR_FONT_SIZE_FACTOR
    import numpy as np

    dpi = PDF_TYPE_DETECTION['ocr_dpi']

    # 延迟导入PaddleOCR
    try:
        from paddleocr import PPStructureV3
    except ImportError:
        try:
            from paddleocr import PPStructure as PPStructureV3
        except ImportError:
            raise ImportError(
                "PaddleOCR未安装，请运行: pip install paddlepaddle paddleocr"
            )

    engine = PPStructureV3(
        show_log=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_seal_recognition=False,
        use_chart_recognition=False,
        use_formula_recognition=True,
        use_table_recognition=True,
    )

    doc = fitz.open(pdf_path)
    elements: List[ContentElement] = []

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        page_width = page.rect.width

        # PDF页面转图像
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        # 转为RGB（去掉alpha通道）
        if pix.n == 4:
            img_array = img_array[:, :, :3]

        # 版面分析
        result = engine(img_array)

        for region in result:
            region_type = region.get('type', 'text')
            bbox = region.get('bbox', [0, 0, 0, 0])

            # 将像素坐标转为PDF点坐标（1 inch = 72pt）
            scale = 72.0 / dpi
            pdf_bbox = [b * scale for b in bbox]

            if region_type in ('title', 'text'):
                # 提取文本
                res = region.get('res', [])
                if isinstance(res, list):
                    texts = []
                    for item in res:
                        if isinstance(item, dict):
                            texts.append(item.get('text', ''))
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            texts.append(item[1][0] if isinstance(item[1], (list, tuple)) else str(item[1]))
                    text = ' '.join(texts).strip()
                elif isinstance(res, str):
                    text = res
                else:
                    text = str(res)

                if not text:
                    continue

                # 估算字号（基于bbox高度）
                bbox_height = bbox[3] - bbox[1]
                est_font_size = bbox_height * 72 / dpi * OCR_FONT_SIZE_FACTOR

                elements.append(ContentElement(
                    type='text' if region_type == 'text' else 'text',
                    text=text,
                    bbox=pdf_bbox,
                    page_num=page_num,
                    font_name=None,    # OCR无法获取
                    font_size=est_font_size,
                    is_bold=None,      # OCR无法获取
                    is_italic=None,
                    alignment=_detect_alignment(pdf_bbox, page_width * scale),
                ))

            elif region_type == 'table':
                # 表格区域
                res = region.get('res', {})
                html = ''
                if isinstance(res, dict):
                    html = res.get('html', '')
                elif isinstance(res, str):
                    html = res

                if html:
                    elements.append(ContentElement(
                        type='table',
                        text='',
                        bbox=pdf_bbox,
                        page_num=page_num,
                        html=html,
                    ))

            elif region_type in ('figure', 'image'):
                # 图片区域：裁剪保存
                x1, y1, x2, y2 = [int(b) for b in bbox]
                if x2 > x1 and y2 > y1:
                    import cv2
                    crop_img = img_array[y1:y2, x1:x2]
                    success, img_buffer = cv2.imencode('.png', crop_img)
                    if success:
                        elements.append(ContentElement(
                            type='image',
                            text='',
                            bbox=pdf_bbox,
                            page_num=page_num,
                            image_data=img_buffer.tobytes(),
                        ))

    doc.close()

    # 按页面和位置排序
    elements.sort(key=lambda e: (e.page_num, e.bbox[1] if e.bbox else 0,
                                  e.bbox[0] if e.bbox else 0))
    return elements
