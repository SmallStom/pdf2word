# -*- coding: utf-8 -*-
"""PDF内容提取模块 - 基于 PP-StructureV3 版面分析的统一提取

策略：
- 所有 PDF 都通过 PP-StructureV3 做版面分析（title/text/table/figure）
- 同时用 PyMuPDF 提取每页的精细 span 信息（字体名/字号/粗斜体）
- 将两者结果按空间位置合并：版面区域给出"是什么"，PyMuPDF 给出"长什么样"
- 原生 PDF 和扫描件走同一条路径，仅 PDF→图片 时使用的 DPI 不同（影响 OCR 精度/速度）
"""

import re
import io
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

import fitz  # PyMuPDF


# ============================================================
# 统一数据结构
# ============================================================
@dataclass
class ContentElement:
    """PDF内容元素的统一表示"""
    type: str  # 'text', 'heading', 'title', 'table', 'image', 'figure'
    text: str = ''
    bbox: List[float] = field(default_factory=list)  # [x1, y1, x2, y2] in PDF points
    page_num: int = 0

    # 样式信息（来自PyMuPDF；扫描件或缺失时为None）
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
# PP-StructureV3 模型初始化（延迟加载，避免导入即报错）
# ============================================================
_PP_STRUCTURE_ENGINE = None


def _get_paddle_engine(device: str):
    """获取或创建 PP-StructureV3 引擎（单例）"""
    global _PP_STRUCTURE_ENGINE
    if _PP_STRUCTURE_ENGINE is not None:
        return _PP_STRUCTURE_ENGINE

    from config import PADDLE_GPU_MEMORY_GB, PADDLE_GPU_ID, PADDLE_DEVICE
    _setup_gpu_memory_limit()

    # PaddleOCR 3.x 推荐入口
    try:
        from paddleocr import PPStructureV3
    except ImportError as e:
        raise ImportError(
            "未安装 paddleocr，请运行: pip install paddleocr"
        ) from e

    _PP_STRUCTURE_ENGINE = PPStructureV3(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_formula_recognition=False,
        use_table_recognition=True,
        use_chart_recognition=False,
        lang='ch',
        device=device,
    )
    return _PP_STRUCTURE_ENGINE


def _setup_gpu_memory_limit():
    """设置GPU显存限制（在PaddleOCR初始化前调用）"""
    from config import PADDLE_DEVICE, PADDLE_GPU_MEMORY_GB, PADDLE_GPU_ID
    if PADDLE_DEVICE != 'gpu' or not PADDLE_GPU_MEMORY_GB:
        return
    try:
        import paddle
        if not paddle.device.is_compiled_with_cuda():
            return
        gpu_props = paddle.device.cuda.get_device_properties(PADDLE_GPU_ID)
        total_gb = gpu_props.total_memory / (1024 ** 3)
        fraction = min(PADDLE_GPU_MEMORY_GB / total_gb, 1.0)
        paddle.device.set_memory_fraction(fraction, PADDLE_GPU_ID)
        print(f"[INFO] GPU{PADDLE_GPU_ID} 显存限制: {PADDLE_GPU_MEMORY_GB}GB / {total_gb:.1f}GB")
    except Exception as e:
        print(f"[WARNING] GPU显存限制设置失败: {e}")


# ============================================================
# 兼容旧接口：detect_pdf_type 保留，但已不再分支
# ============================================================
def detect_pdf_type(pdf_path: str) -> str:
    """保留接口：现在所有 PDF 都走版面分析路径"""
    return 'layout'  # 统一标记


# ============================================================
# 主入口：基于版面分析的提取
# ============================================================
def extract_with_layout_analysis(pdf_path: str, dpi: int = 200) -> List[ContentElement]:
    """对 PDF 的每一页执行：
    1. PyMuPDF 把页面渲染为高分辨率图像（供 PP-StructureV3 识别）
    2. PyMuPDF 提取页面文字 spans（供字体/字号/粗体推断）
    3. PP-StructureV3 对图像做版面分析 → 给出 region (title/text/table/figure)
    4. 按 region 聚合 spans 内的字体信息
    """
    from config import PADDLE_DEVICE, PADDLE_GPU_ID

    device = f'gpu:{PADDLE_GPU_ID}' if PADDLE_DEVICE == 'gpu' else 'cpu'
    engine = _get_paddle_engine(device)

    import numpy as np

    doc = fitz.open(pdf_path)
    elements: List[ContentElement] = []

    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        page_width = page.rect.width
        page_height = page.rect.height
        scale = dpi / 72.0  # 像素/pt 的换算系数

        # 1. 渲染为图像
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        if pix.n == 4:
            img_array = img_array[:, :, :3]

        # 2. PyMuPDF 提取 spans（按行聚合）
        page_spans = _extract_page_spans(page)

        # 3. PP-StructureV3 版面分析
        # v3 predict() 接受 numpy array 或文件路径
        try:
            layout_result = engine.predict(img_array)
        except TypeError:
            # 兼容性回退
            layout_result = engine(img_array)

        # 4. 遍历版面分析结果，转换为 ContentElement
        for region in layout_result:
            elem = _convert_region(
                region, page_num, page_width, page_height,
                scale, img_array, page_spans,
            )
            if elem is not None:
                elements.append(elem)

    doc.close()

    # 按页面和位置排序
    elements.sort(key=lambda e: (e.page_num, e.bbox[1] if e.bbox else 0,
                                  e.bbox[0] if e.bbox else 0))
    return elements


# ============================================================
# PyMuPDF span 提取
# ============================================================
def _extract_page_spans(page) -> List[Dict]:
    """提取一页内所有 span 的字体信息

    返回列表，每个元素：
    {
        'text': str,
        'bbox': [x1, y1, x2, y2],  # in PDF points
        'font': str,                # 字体名
        'size': float,              # 字号 (pt)
        'flags': int,               # 粗体/斜体标志
        'is_bold': bool,
        'is_italic': bool,
    }
    """
    spans = []
    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                flags = span.get("flags", 0)
                spans.append({
                    'text': text,
                    'bbox': list(span.get("bbox", [0, 0, 0, 0])),
                    'font': span.get("font", ""),
                    'size': float(span.get("size", 0)),
                    'flags': flags,
                    'is_bold': bool(flags & 16),
                    'is_italic': bool(flags & 2),
                })
    return spans


def _find_spans_in_region(spans: List[Dict], region_bbox: List[float],
                           overlap_threshold: float = 0.5) -> List[Dict]:
    """找出落在 region 内的所有 spans（按重叠面积判断）"""
    rx1, ry1, rx2, ry2 = region_bbox
    region_area = (rx2 - rx1) * (ry2 - ry1)
    if region_area <= 0:
        return []

    matched = []
    for span in spans:
        sx1, sy1, sx2, sy2 = span['bbox']
        span_area = (sx2 - sx1) * (sy2 - sy1)
        if span_area <= 0:
            continue
        # 计算重叠
        ox1 = max(rx1, sx1)
        oy1 = max(ry1, sy1)
        ox2 = min(rx2, sx2)
        oy2 = min(ry2, sy2)
        if ox1 < ox2 and oy1 < oy2:
            overlap = (ox2 - ox1) * (oy2 - oy1)
            if overlap / span_area > overlap_threshold:
                matched.append(span)
    return matched


def _aggregate_span_style(spans: List[Dict]) -> Dict:
    """聚合一组 span 的样式信息：取最显著的字体/字号"""
    if not spans:
        return {'font_name': None, 'font_size': None,
                'is_bold': None, 'is_italic': None}

    # 按文本长度加权统计字号出现频次
    size_counter: Dict[float, int] = {}
    for s in spans:
        sz = round(s['size'], 1)
        size_counter[sz] = size_counter.get(sz, 0) + len(s['text'])

    dominant_size = max(size_counter, key=size_counter.get) if size_counter else 0

    # 取该字号对应的 font 名（多数派）
    fonts_at_size = [s['font'] for s in spans if round(s['size'], 1) == dominant_size]
    font_name = max(set(fonts_at_size), key=fonts_at_size.count) if fonts_at_size else None

    # 粗体/斜体：取多数派
    bold_count = sum(1 for s in spans if s['is_bold'])
    italic_count = sum(1 for s in spans if s['is_italic'])
    is_bold = bold_count > len(spans) / 2 if spans else None
    is_italic = italic_count > len(spans) / 2 if spans else None

    return {
        'font_name': font_name,
        'font_size': dominant_size if dominant_size > 0 else None,
        'is_bold': is_bold,
        'is_italic': is_italic,
    }


# ============================================================
# 版面区域 → ContentElement
# ============================================================
def _convert_region(region: Dict, page_num: int, page_width: float,
                     page_height: float, scale: float,
                     img_array, page_spans: List[Dict]) -> Optional[ContentElement]:
    """把 PP-StructureV3 的一个 region 转成 ContentElement"""
    region_type = region.get('type', 'text')

    # bbox 是像素坐标，需要转回 PDF points
    pixel_bbox = region.get('bbox', [0, 0, 0, 0])
    if len(pixel_bbox) != 4:
        return None
    pdf_bbox = [b / scale for b in pixel_bbox]

    # 找该 region 内的 spans
    matched_spans = _find_spans_in_region(page_spans, pdf_bbox)
    style = _aggregate_span_style(matched_spans)

    # 提取文本
    text, html = _extract_region_text(region, region_type, img_array, pixel_bbox, scale)

    if region_type in ('title', 'paragraph_title', 'heading'):
        return ContentElement(
            type='heading',
            text=text,
            bbox=pdf_bbox,
            page_num=page_num,
            font_name=style['font_name'],
            font_size=style['font_size'],
            is_bold=style['is_bold'],
            is_italic=style['is_italic'],
            alignment=_detect_alignment(pdf_bbox, page_width),
        )

    elif region_type in ('text', 'paragraph', 'content', 'reference',
                          'abstract', 'algorithm', 'footer', 'header'):
        # 页眉页脚也按 text 处理，由后续 style_mapper/word_generator 决定是否过滤
        if not text:
            return None
        return ContentElement(
            type='text',
            text=text,
            bbox=pdf_bbox,
            page_num=page_num,
            font_name=style['font_name'],
            font_size=style['font_size'],
            is_bold=style['is_bold'],
            is_italic=style['is_italic'],
            alignment=_detect_alignment(pdf_bbox, page_width),
        )

    elif region_type in ('table', 'wired_table', 'wireless_table'):
        if not html:
            return None
        return ContentElement(
            type='table',
            text='',
            bbox=pdf_bbox,
            page_num=page_num,
            html=html,
        )

    elif region_type in ('figure', 'image', 'chart'):
        # 裁剪图片区域
        x1, y1, x2, y2 = [int(b) for b in pixel_bbox]
        if x2 <= x1 or y2 <= y1:
            return None
        try:
            import cv2
            crop = img_array[y1:y2, x1:x2]
            success, buf = cv2.imencode('.png', crop)
            if not success:
                return None
            return ContentElement(
                type='image',
                text='',
                bbox=pdf_bbox,
                page_num=page_num,
                image_data=buf.tobytes(),
            )
        except Exception:
            return None

    # 其他类型 (footnote, formula 等) 一律丢弃
    return None


def _extract_region_text(region: Dict, region_type: str, img_array,
                          pixel_bbox: List[float], scale: float) -> Tuple[str, str]:
    """从 region 中提取文本（text/title）或 HTML（table）

    PP-StructureV3 v3 返回结构：
    - text/title: region['res'] = {'rec_text': '完整文本', 'rec_boxes': [...], ...}
                   或 region['res'] = [(box, (text, score)), ...]  (v2 兼容)
    - table: region['res'] = {'html': '<table>...</table>', ...}
    """
    res = region.get('res')

    # ---- text/title 提取 ----
    if region_type in ('title', 'paragraph_title', 'heading',
                        'text', 'paragraph', 'content'):
        if res is None:
            return '', ''

        # v3 格式：dict 含 rec_text
        if isinstance(res, dict):
            text = res.get('rec_text', '') or res.get('text', '')
            return text.strip(), ''

        # v2 格式：list of (box, (text, score))
        if isinstance(res, list):
            texts = []
            for item in res:
                if isinstance(item, dict):
                    texts.append(item.get('text', ''))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    txt = item[1]
                    if isinstance(txt, (list, tuple)) and len(txt) >= 1:
                        texts.append(str(txt[0]))
                    else:
                        texts.append(str(txt))
            return ' '.join(texts).strip(), ''

        return str(res).strip(), ''

    # ---- table 提取 ----
    if region_type in ('table', 'wired_table', 'wireless_table'):
        if isinstance(res, dict):
            html = res.get('html', '')
            return '', html
        if isinstance(res, str):
            return '', res

    return '', ''


# ============================================================
# 对齐方式检测
# ============================================================
def _detect_alignment(bbox, page_width) -> str:
    x1, y1, x2, y2 = bbox
    left = x1
    right = page_width - x2
    elem_width = x2 - x1

    if abs(left - right) < 50 and elem_width < page_width * 0.8:
        return 'center'
    elif right < 50 and left > 100:
        return 'right'
    else:
        return 'left'


# ============================================================
# 兼容旧接口（保留以防外部调用）
# ============================================================
def extract_native_pdf(pdf_path: str) -> List[ContentElement]:
    """兼容旧接口：现在等价于版面分析路径"""
    return extract_with_layout_analysis(pdf_path, dpi=200)


def extract_scanned_pdf(pdf_path: str) -> List[ContentElement]:
    """兼容旧接口：扫描件也用同一路径，仅 DPI 略高以获得更精确 OCR"""
    return extract_with_layout_analysis(pdf_path, dpi=300)
