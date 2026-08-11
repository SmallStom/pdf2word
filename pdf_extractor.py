# -*- coding: utf-8 -*-
"""PDF内容提取模块 - 基于 PP-StructureV3 版面分析

每页用 PyMuPDF 渲染成临时 PNG，传给 PP-StructureV3.predict() 做版面分析，
然后从 LayoutParsingResultV2 提取结构化信息。
"""

import os
import re
import io
import time
import tempfile
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


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
    alignment: Optional[str] = None


# ============================================================
# GPU 环境预设（必须在 import paddle 之前）
# ============================================================
def _pre_setup_gpu_env():
    """在 import paddle 之前预设 GPU 显存环境变量"""
    import os
    from config import PADDLE_DEVICE, PADDLE_GPU_MEMORY_GB, PADDLE_GPU_ID

    if PADDLE_DEVICE != 'gpu' or not PADDLE_GPU_MEMORY_GB:
        return

    os.environ['FLAGS_selected_gpus'] = str(PADDLE_GPU_ID)
    os.environ.setdefault('CUDA_VISIBLE_DEVICES', str(PADDLE_GPU_ID))

    # 保守估算：按 24GB 卡算
    try:
        fraction = min(PADDLE_GPU_MEMORY_GB / 24.0, 0.95)
    except Exception:
        fraction = 0.5
    os.environ['FLAGS_fraction_of_gpu_memory_to_use'] = str(fraction)
    logger.info(f"[INFO] 预设 GPU{PADDLE_GPU_ID} 显存比例: {fraction:.1%} ({PADDLE_GPU_MEMORY_GB}GB)")


def _print_gpu_info():
    """打印当前 GPU 信息（paddle 已 import 后）"""
    try:
        from config import PADDLE_DEVICE, PADDLE_GPU_MEMORY_GB, PADDLE_GPU_ID
        if PADDLE_DEVICE != 'gpu':
            return
        import paddle
        if not paddle.device.is_compiled_with_cuda():
            logger.warning("[WARNING] PaddlePaddle未编译GPU支持，使用CPU")
            return
        gpu_props = paddle.device.cuda.get_device_properties(PADDLE_GPU_ID)
        total_gb = gpu_props.total_memory / (1024 ** 3)
        logger.info(f"[INFO] GPU{PADDLE_GPU_ID} ({gpu_props.name}) 总显存 {total_gb:.1f}GB, "
                    f"限制 {PADDLE_GPU_MEMORY_GB}GB")
    except Exception as e:
        logger.warning(f"[WARNING] GPU信息查询失败: {e}")


# ============================================================
# PP-StructureV3 引擎（单例）
# ============================================================
_PP_STRUCTURE_ENGINE = None


def _get_paddle_engine(device: str):
    """获取或创建 PP-StructureV3 引擎（单例）"""
    import sys
    global _PP_STRUCTURE_ENGINE
    print(f"[ENGINE-0] _get_paddle_engine called, device={device}, existing={_PP_STRUCTURE_ENGINE is not None}", flush=True)
    if _PP_STRUCTURE_ENGINE is not None:
        return _PP_STRUCTURE_ENGINE

    from config import PADDLE_GPU_ID, PADDLE_DEVICE
    print(f"[ENGINE-1] printing GPU info", flush=True)
    _print_gpu_info()
    print(f"[ENGINE-1] GPU info printed", flush=True)

    try:
        from paddleocr import PPStructureV3
        print(f"[ENGINE-2] PPStructureV3 imported", flush=True)
    except ImportError as e:
        print(f"[ENGINE-2] import FAILED: {e}", flush=True)
        raise ImportError("未安装 paddleocr，请运行: pip install paddleocr") from e

    print(f"[ENGINE-3] initializing PPStructureV3 (this may take minutes on first run)...", flush=True)
    _PP_STRUCTURE_ENGINE = PPStructureV3(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_seal_recognition=False,
        use_formula_recognition=False,
        use_table_recognition=True,
        use_chart_recognition=False,
        use_region_detection=True,
        lang='ch',
        device=device,
    )
    print(f"[ENGINE-3] PPStructureV3 initialized", flush=True)
    logger.info("[INFO] PP-StructureV3 初始化完成")
    return _PP_STRUCTURE_ENGINE


# ============================================================
# 兼容旧接口
# ============================================================
def detect_pdf_type(pdf_path: str) -> str:
    """保留接口：现在所有 PDF 都走版面分析路径"""
    return 'layout'


# ============================================================
# 主入口：基于版面分析的提取
# ============================================================
def extract_with_layout_analysis(pdf_path: str, dpi: int = 200) -> List[ContentElement]:
    """对 PDF 的每一页执行：
    1. PyMuPDF 把页面渲染为临时 PNG 文件
    2. PyMuPDF 提取页面文字 spans（用于字体推断）
    3. PP-StructureV3.predict() 对图像做版面分析
    4. 把 result 转成 ContentElement
    """
    import sys
    from config import PADDLE_DEVICE, PADDLE_GPU_ID

    print(f"[EXT-0] extract_with_layout_analysis entered: {pdf_path}, dpi={dpi}", flush=True)
    print(f"[EXT-0] PADDLE_DEVICE={PADDLE_DEVICE}, PADDLE_GPU_ID={PADDLE_GPU_ID}", flush=True)

    device = f'gpu:{PADDLE_GPU_ID}' if PADDLE_DEVICE == 'gpu' else 'cpu'
    print(f"[EXT-1] creating engine with device={device}", flush=True)
    engine = _get_paddle_engine(device)
    print(f"[EXT-1] engine ready", flush=True)

    doc = fitz.open(pdf_path)
    elements: List[ContentElement] = []
    total_pages = len(doc)
    print(f"[EXT-2] PDF opened, {total_pages} pages", flush=True)

    # 限制最大处理页数（用于快速验证/调试）
    from config import MAX_PAGES
    if MAX_PAGES and MAX_PAGES > 0 and total_pages > MAX_PAGES:
        print(f"[EXT-2] MAX_PAGES={MAX_PAGES} 限制生效，只处理前 {MAX_PAGES} 页（总 {total_pages} 页）", flush=True)
        total_pages = MAX_PAGES

    # 临时目录存放每页的 PNG
    tmp_dir = tempfile.mkdtemp(prefix='pdf2word_')
    logger.info(f"[INFO] 临时目录: {tmp_dir}")

    try:
        for page_num in range(total_pages):
            page = doc.load_page(page_num)
            page_width = page.rect.width
            page_height = page.rect.height
            scale = dpi / 72.0

            # 1. 渲染为 PNG 文件
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_path = os.path.join(tmp_dir, f'page_{page_num:04d}.png')
            pix.save(img_path)

            # 2. PyMuPDF 提取 spans（字体/字号/粗体）
            page_spans = _extract_page_spans(page)

            # 3. PP-StructureV3 版面分析（接受文件路径）
            t0 = time.time()
            try:
                result_iter = engine.predict(img_path)
                # v3 返回 generator，每个元素是整页的 LayoutParsingResultV2
                results = list(result_iter)
            except Exception as e:
                logger.error(f"[ERROR] 第{page_num+1}页版面分析失败: {e}")
                # 打印前 1 行用于诊断
                import traceback
                logger.error(traceback.format_exc()[:2000])
                continue
            elapsed = time.time() - t0
            logger.info(f"[INFO] 第{page_num+1}/{total_pages}页 用时 {elapsed:.1f}s, "
                        f"识别到 {len(results)} 个版面结果")

            if not results:
                # 调试：打印 res 类型和属性
                logger.warning(f"[WARN] 第{page_num+1}页无版面结果，跳过")
                continue

            # 5. 从每个 res (整页) 中提取 ContentElement
            for res in results:
                page_elems = _parse_layout_result(
                    res, page_num, page_width, page_height, scale, page_spans
                )
                elements.extend(page_elems)

    finally:
        # 清理临时文件
        try:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        doc.close()

    # 按页面和位置排序
    elements.sort(key=lambda e: (e.page_num,
                                  e.bbox[1] if e.bbox else 0,
                                  e.bbox[0] if e.bbox else 0))
    return elements


# ============================================================
# 解析 PP-StructureV3 的 LayoutParsingResultV2 结果
# ============================================================
def _parse_layout_result(res, page_num: int, page_width: float,
                          page_height: float, scale: float,
                          page_spans: List[Dict]) -> List[ContentElement]:
    """从 LayoutParsingResultV2 解析出 ContentElement 列表

    res 可能是以下之一：
    1. LayoutParsingResultV2 对象（有 _to_json() / json 属性）
    2. dict（早期版本）
    """
    # 先尝试拿到 dict/json 形式
    if hasattr(res, 'json'):
        try:
            data = res.json
            if isinstance(data, str):
                import json
                data = json.loads(data)
        except Exception:
            data = None
    elif hasattr(res, '_to_json'):
        try:
            data = res._to_json()
        except Exception:
            data = None
    elif isinstance(res, dict):
        data = res
    else:
        logger.warning(f"[WARN] 未知的 res 类型: {type(res)}")
        return []

    if not data:
        # 第一次失败时打印 res 详细信息（仅 page 0）
        if page_num == 0:
            logger.warning(f"[DEBUG page0] res type: {type(res).__name__}")
            if hasattr(res, '__dict__'):
                logger.warning(f"[DEBUG page0] res attrs: {list(res.__dict__.keys())[:15]}")
            if hasattr(res, 'keys'):
                try:
                    logger.warning(f"[DEBUG page0] res keys: {list(res.keys())[:15]}")
                except Exception:
                    pass
            # 尝试常见属性
            for attr in ['json', 'markdown', 'html', 'rec_text', 'pred_html', 'res', '_to_json']:
                if hasattr(res, attr):
                    try:
                        v = getattr(res, attr)
                        if callable(v):
                            try:
                                v = v()
                            except Exception:
                                v = '<callable err>'
                        preview = str(v)[:200]
                        logger.warning(f"[DEBUG page0] res.{attr} = {preview}")
                    except Exception as e:
                        logger.warning(f"[DEBUG page0] res.{attr} err: {e}")
        logger.warning(f"[WARN] 无法从 res 解析数据")
        return []

    # 真实的版面数据在 data['res']['parsing_res_list'] 里
    # 顶层是 {'res': {'parsing_res_list': [...], 'layout_det_res': {...}}}
    inner = data.get('res', data)

    elements: List[ContentElement] = []

    # ---- 路径 1：parsing_res_list（layout_parsing_v2 格式）----
    if 'parsing_res_list' in inner:
        for block in inner['parsing_res_list']:
            # PP-StructureV3 v3 的字段名:
            #   block_label: 'doc_title' | 'text' | 'paragraph_title' | 'table' | 'figure' | ...
            #   block_content: 文本内容（字符串）
            #   block_bbox: [x1, y1, x2, y2] 像素坐标
            block_type = block.get('block_label') or block.get('type', 'text')
            bbox = block.get('block_bbox') or block.get('bbox', [0, 0, 0, 0])
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue

            # 像素坐标 → PDF 点
            pdf_bbox = [b / scale for b in bbox]

            # 找 region 内的 spans
            matched_spans = _find_spans_in_region(page_spans, pdf_bbox)
            style = _aggregate_span_style(matched_spans)

            # 对齐方式：优先 block 自带，再 span 位置检测
            alignment = _extract_block_alignment(block) \
                or _detect_alignment(pdf_bbox, page_width, matched_spans)

            # 文本内容：v3 字段是 block_content
            text = (block.get('block_content')
                    or block.get('content')
                    or block.get('text')
                    or '')
            # 清理 OCR 错误（半全角混用、连续标点等）
            if text:
                import re as _re
                # 修复全角冒号/斜杠（在 URL 场景）
                text = _re.sub(r'(https?|ftp)\s*[：:]\s*/\s*/\s*', r'\1://', text)
                text = _re.sub(r'([：:])\s*/\s*/\s*', r'://', text)
                # 中文之间的半角空格去掉
                text = _re.sub(r'([\u4e00-\u9fa5])\s+([\u4e00-\u9fa5])', r'\1\2', text)
                # 修复"第X章"后多余空格
                text = _re.sub(r'^(第\s*[零一二三四五六七八九十百千0-9]+\s*[章部分编篇节条款])\s+', r'\1', text)
                # 修复连续标点
                text = _re.sub(r'，\s*，', '，', text)
                text = _re.sub(r'。\s*。', '。', text)
                text = text.strip()

            if block_type in ('title', 'paragraph_title', 'heading', 'doc_title'):
                elements.append(ContentElement(
                    type='heading',
                    text=text,
                    bbox=pdf_bbox,
                    page_num=page_num,
                    font_name=style['font_name'],
                    font_size=style['font_size'],
                    is_bold=style['is_bold'],
                    is_italic=style['is_italic'],
                    alignment=alignment,
                ))
            elif block_type in ('text', 'paragraph', 'content', 'reference',
                                 'abstract', 'algorithm', 'header', 'footer',
                                 'figure_title', 'table_title'):
                elements.append(ContentElement(
                    type='text',
                    text=text,
                    bbox=pdf_bbox,
                    page_num=page_num,
                    font_name=style['font_name'],
                    font_size=style['font_size'],
                    is_bold=style['is_bold'],
                    is_italic=style['is_italic'],
                    alignment=alignment,
                ))
            elif block_type in ('table', 'wired_table', 'wireless_table'):
                # 表格的 HTML 可能在多个字段
                html = (block.get('pred_html')
                        or block.get('html')
                        or '')
                if not html and 'table_ocr_pred' in block:
                    # 拼装简化 HTML
                    html = _build_simple_table_html(block.get('table_ocr_pred', {}))
                # 也可能 block_content 直接是 HTML
                if not html and text and ('<table' in text or '<tr' in text):
                    html = text
                if html:
                    elements.append(ContentElement(
                        type='table', text='', bbox=pdf_bbox, page_num=page_num,
                        html=html,
                    ))
                else:
                    # 没有 html，但有文本内容（可能是表格标题）
                    if text:
                        elements.append(ContentElement(
                            type='text',
                            text=text,
                            bbox=pdf_bbox,
                            page_num=page_num,
                            font_name=style['font_name'],
                            font_size=style['font_size'],
                            is_bold=style['is_bold'],
                            is_italic=style['is_italic'],
                            alignment=alignment,
                        ))
            elif block_type in ('figure', 'image', 'chart'):
                # 跳过图片裁剪（PP-StructureV3 不便直接拿到原图像素）
                # 但图名/图标题可能作为 figure_title 出现，已在上面处理
                pass

    # ---- 路径 2：layout_det_res（layout_detection 格式）----
    elif 'layout_det_res' in inner:
        boxes = inner['layout_det_res'].get('boxes', [])
        for box in boxes:
            label = box.get('label', 'text')
            bbox = box.get('coordinate') or box.get('bbox', [0, 0, 0, 0])
            if not isinstance(bbox, list) or len(bbox) != 4:
                continue
            pdf_bbox = [b / scale for b in bbox]
            matched_spans = _find_spans_in_region(page_spans, pdf_bbox)
            style = _aggregate_span_style(matched_spans)
            text = box.get('text', '')
            box_alignment = _extract_block_alignment(box) \
                or _detect_alignment(pdf_bbox, page_width, matched_spans)

            if label in ('title', 'paragraph_title', 'heading', 'doc_title'):
                t = 'heading'
            elif label in ('text', 'paragraph', 'content'):
                t = 'text'
            elif label in ('table',):
                t = 'table'
            elif label in ('figure', 'image'):
                t = 'image'
            else:
                continue

            elem = ContentElement(
                type=t, text=text, bbox=pdf_bbox, page_num=page_num,
                font_name=style['font_name'], font_size=style['font_size'],
                is_bold=style['is_bold'], is_italic=style['is_italic'],
                alignment=box_alignment,
            )
            elements.append(elem)

    else:
        logger.warning(f"[WARN] 未知的 res 结构，keys: {list(inner.keys())[:10]}")

    return elements


def _build_simple_table_html(table_ocr_pred: Dict) -> str:
    """从 OCR 预测结果拼装简化 HTML 表格"""
    rec_texts = table_ocr_pred.get('rec_texts', [])
    rec_boxes = table_ocr_pred.get('rec_boxes', [])
    if not rec_texts:
        return ''
    # 简化处理：把每行文本作为一行（这里仅作占位）
    rows_html = ''
    for txt in rec_texts:
        rows_html += f'<tr><td>{txt}</td></tr>'
    return f'<table>{rows_html}</table>'


# ============================================================
# PyMuPDF span 提取
# ============================================================
def _extract_page_spans(page) -> List[Dict]:
    """提取一页内所有 span 的字体信息"""
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
    """找出落在 region 内的所有 spans"""
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

    size_counter: Dict[float, int] = {}
    for s in spans:
        sz = round(s['size'], 1)
        size_counter[sz] = size_counter.get(sz, 0) + len(s['text'])

    dominant_size = max(size_counter, key=size_counter.get) if size_counter else 0

    fonts_at_size = [s['font'] for s in spans if round(s['size'], 1) == dominant_size]
    font_name = max(set(fonts_at_size), key=fonts_at_size.count) if fonts_at_size else None

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


def _detect_alignment(bbox, page_width, spans: List[Dict] = None) -> str:
    """根据 bbox + 内部 span 位置 检测对齐方式

    算法（更可靠）：
    1. 如果传了 spans，用所有 span 的 left/right 中位数算对齐
       - 文本居中：左右边距大致相等
       - 文本左对齐：左 margin 小，右 margin 大
       - 文本右对齐：左 margin 大，右 margin 小
    2. 否则用 bbox 粗略判断
    """
    if spans:
        return _detect_alignment_from_spans(spans, page_width)

    x1, y1, x2, y2 = bbox
    left = x1
    right = page_width - x2
    elem_width = x2 - x1

    # 容差：左右 margin 相差 < 5% 页宽 算居中
    tol = page_width * 0.05
    if abs(left - right) < tol and elem_width < page_width * 0.85:
        return 'center'
    elif right < tol and left > tol:
        return 'right'
    else:
        return 'left'


def _detect_alignment_from_spans(spans: List[Dict], page_width: float) -> str:
    """用 span 实际位置判断对齐（更精确）

    spans 是 PyMuPDF 抽出的 span 列表，每条带 'bbox' [x1, y1, x2, y2]
    """
    if not spans:
        return 'left'

    # 取所有 span 的左边界和右边界
    lefts = [s['bbox'][0] for s in spans]
    rights = [s['bbox'][2] for s in spans]
    # 用最左和最右（最保守的检测）
    min_left = min(lefts)
    max_right = max(rights)

    left_margin = min_left
    right_margin = page_width - max_right

    # 容差：5% 页宽 = 约 30pt
    tol = page_width * 0.05

    if abs(left_margin - right_margin) < tol:
        return 'center'
    elif right_margin < tol and left_margin > tol:
        return 'right'
    else:
        return 'left'


def _extract_block_alignment(block: Dict) -> Optional[str]:
    """从 PP-StructureV3 的 block 字段直接读 alignment

    v3 的 block 可能含 'text_align' / 'align' / 'alignment' 字段
    """
    if not block:
        return None
    for key in ('text_align', 'align', 'alignment', 'horizontal_alignment'):
        val = block.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            val_lower = val.lower()
            if val_lower in ('center', 'centre', 'middle', '居中'):
                return 'center'
            if val_lower in ('right', 'right_align', '右对齐'):
                return 'right'
            if val_lower in ('left', 'left_align', '左对齐'):
                return 'left'
        elif isinstance(val, (int, float)):
            # 一些模型用 0=left, 1=center, 2=right
            if val == 1:
                return 'center'
            elif val == 2:
                return 'right'
            elif val == 0:
                return 'left'
    return None


# ============================================================
# 兼容旧接口
# ============================================================
def extract_native_pdf(pdf_path: str) -> List[ContentElement]:
    return extract_with_layout_analysis(pdf_path, dpi=200)


def extract_scanned_pdf(pdf_path: str) -> List[ContentElement]:
    return extract_with_layout_analysis(pdf_path, dpi=300)
