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
    type: str  # 'text', 'heading', 'title', 'table', 'image', 'figure', 'toc'
    text: str = ''
    bbox: List[float] = field(default_factory=list)  # [x1, y1, x2, y2] in PDF points
    page_num: int = 0

    # 样式信息（来自PyMuPDF；扫描件或缺失时为None）
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    is_bold: Optional[bool] = None
    is_italic: Optional[bool] = None
    color: Optional[int] = None  # sRGB整数，如0x000000黑

    # 表格专用
    html: Optional[str] = None

    # 图片专用
    image_data: Optional[bytes] = None

    # 后处理填充
    heading_level: Optional[int] = None
    mapped_size: Optional[float] = None
    alignment: Optional[str] = None

    # ---- 数字版PDF专用（extract_digital_pdf）----
    # 行内run列表：保留段落内不同字体/字号/颜色/加粗的片段（如"CAB-A"、超链接）
    # 每项: {'text': str, 'size': float, 'bold': bool, 'italic': bool, 'color': int}
    runs: Optional[List[Dict]] = None
    # 目录条目：标题 + 右对齐页码（制表位+引导符重建）
    is_toc: bool = False
    toc_title: str = ''
    toc_page_no: str = ''
    # 相对正文左边界的左缩进（pt），目录层级/列表缩进用
    left_indent_pt: float = 0.0
    # 该元素所在页是否为横版（宽>高），Word生成时切换分节方向
    is_landscape: bool = False


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

            # 2.5 PyMuPDF 段落级文本块（用于合并被版面分析切碎的片段）
            page_blocks = _extract_page_text_blocks(page)

            # 2.6 PyMuPDF 表格检测（数字版PDF可用，用于纠正OCR表格HTML）
            page_tables = _extract_page_tables(page)

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

            # 5. 从每个 res (整页) 中提取 ContentElement，并按 PyMuPDF 段落块合并碎片
            for res in results:
                page_elems = _parse_layout_result(
                    res, page_num, page_width, page_height, scale,
                    page_spans, page_tables, page_blocks
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
                          page_spans: List[Dict],
                          page_tables: List[Dict] = None,
                          page_blocks: List[Dict] = None) -> List[ContentElement]:
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

            # 文本内容：v3 字段是 block_content
            ocr_text = (block.get('block_content')
                        or block.get('content')
                        or block.get('text')
                        or '')
            # 数字版PDF优先用 PyMuPDF span 文本（精确保留空格/下划线/全半角），
            # OCR 可能丢失或错误处理这些字符；扫描件无 span，退回 OCR 文本
            span_text = _build_span_text(matched_spans)
            text, used_span_text = _choose_best_text(ocr_text, span_text)
            # 清理 OCR 错误（半全角混用、连续标点等）+ 统一换行格式
            if text:
                import re as _re
                # 统一换行格式：\r\n / \r 都归一为 \n
                text = _re.sub(r'\r\n?', '\n', text)
                # 修复全角冒号/斜杠（在 URL 场景）
                text = _re.sub(r'(https?|ftp)\s*[：:]\s*/\s*/\s*', r'\1://', text)
                text = _re.sub(r'([：:])\s*/\s*/\s*', r'://', text)
                # "1.1" 后面多余空格
                text = _re.sub(r'^(\d+(?:\.\d+)+)\s{2,}', r'\1 ', text)
                # 修复连续标点
                text = _re.sub(r'，\s*，', '，', text)
                text = _re.sub(r'。\s*。', '。', text)
                # 换行：连续多个 \n 压缩为一个，行首行尾去空白
                text = _re.sub(r'\n\s*\n+', '\n', text)
                text = '\n'.join(line.strip() for line in text.split('\n'))
                text = text.strip()

            # 对齐方式：多信号综合判断（必须在 text 赋值之后调用）
            # 把 PyMuPDF block 的 bbox 优先用上（段落级，比 PP-StructureV3 region bbox 更准）
            pymupdf_bbox = _get_pymupdf_block_bbox(matched_spans) if matched_spans else None
            effective_bbox = pymupdf_bbox if pymupdf_bbox else pdf_bbox
            alignment = _detect_alignment(
                effective_bbox, page_width, matched_spans,
                text=text, block_label=block_type, block=block
            )

            # 关键：只有真正的"文档标题"才强制 type=heading（如"招标文件"封面大字）
            # "paragraph_title" 是 PP-StructureV3 对"段落小标题"的判断
            # 但它经常误把"1. 招标条件"这种大编号列表项当 paragraph_title
            # 所以 paragraph_title 不强制 heading，让 style_mapper 二次判断
            if block_type in ('title', 'doc_title', 'heading'):
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
                                 'figure_title', 'table_title',
                                 'paragraph_title'):  # 让 paragraph_title 走 text 路径，由 style_mapper 判断
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
                # 数字版PDF：若 PyMuPDF 检测到对应的真实表格，优先用它生成更准的 HTML
                # （OCR 表格识别常出错：漏列、错字、单元格错位）
                pt_table = _match_pymupdf_table(pdf_bbox, page_tables)
                if pt_table and pt_table.get('rows'):
                    html = _rows_to_html(pt_table['rows'])
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
            box_alignment = _detect_alignment(
                pdf_bbox, page_width, matched_spans,
                text=text, block_label=label, block=box
            )

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

    # 6. 按 PyMuPDF 段落级文本块合并被 PP-StructureV3 切碎的文本片段
    if page_blocks:
        elements = _merge_elements_by_text_blocks(elements, page_blocks, page_width)

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


def _extract_page_tables(page) -> List[Dict]:
    """用 PyMuPDF 检测数字版PDF的表格（结构通常比OCR准确）"""
    tables = []
    try:
        finder = page.find_tables()
        for t in finder.tables:
            try:
                rows = t.extract()
            except Exception:
                rows = []
            if not rows:
                continue
            # t.bbox 是 PyMuPDF Rect，转成 list
            bb = list(t.bbox)
            if len(bb) != 4:
                continue
            tables.append({'bbox': bb, 'rows': rows})
    except Exception:
        pass
    return tables


def _match_pymupdf_table(bbox, tables: List[Dict]) -> Optional[Dict]:
    """按 bbox 重叠率匹配 PyMuPDF 检测到的表格（返回重叠度最高且>=0.5的）"""
    if not tables or not bbox or len(bbox) != 4:
        return None
    rx1, ry1, rx2, ry2 = bbox
    area = (rx2 - rx1) * (ry2 - ry1)
    if area <= 0:
        return None
    best = None
    best_ratio = 0.0
    for t in tables:
        bx1, by1, bx2, by2 = t['bbox']
        ox1, oy1 = max(rx1, bx1), max(ry1, by1)
        ox2, oy2 = min(rx2, bx2), min(ry2, by2)
        if ox1 < ox2 and oy1 < oy2:
            inter = (ox2 - ox1) * (oy2 - oy1)
            ratio = inter / area
            if ratio > best_ratio:
                best, best_ratio = t, ratio
    return best if best_ratio >= 0.5 else None


def _rows_to_html(rows: List[list]) -> str:
    """把表格行数据转成简单 HTML 表格

    单元格内换行保留（生成Word时渲染为软换行，保持单元格内部结构）。
    """
    html_rows = []
    for row in rows:
        cells = []
        for cell in row:
            if cell is None:
                cell = ''
            # 压缩换行周围的空白，保留行结构
            cell_text = re.sub(r'[ \t]*\n[ \t]*', '\n', str(cell).strip())
            # 避免把 HTML 标签当文本
            cell_text = cell_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            cells.append(f'<td>{cell_text}</td>')
        html_rows.append('<tr>' + ''.join(cells) + '</tr>')
    return '<table>' + ''.join(html_rows) + '</table>'


def _build_span_text(spans: List[Dict]) -> str:
    """把 PyMuPDF span 按行聚合成段落文本（数字版PDF的精确文本）

    PyMuPDF span 直接来自 PDF 内容流，能完整保留空格、下划线、全半角字符。
    按视觉行分组（y坐标相近），行内按 x 坐标从左到右拼接。
    行与行之间用换行('\n')连接，保留原文行的结构（OCR 的 block_content 也是
    用 '\n' 分隔行的，两者保持一致），而不是用空格把多行并成一行。
    """
    if not spans:
        return ''
    # 按 y 坐标分组（同一视觉行）
    line_map: Dict[float, List[Dict]] = {}
    for s in spans:
        key = round(s['bbox'][1], 1)
        line_map.setdefault(key, []).append(s)

    parts = []
    for y in sorted(line_map):
        line_spans = sorted(line_map[y], key=lambda s: s['bbox'][0])
        # 行内逐个 span 拼接（保留 span 内部文本原样，空格保留）
        line_text = ''.join(s['text'] for s in line_spans)
        line_text = line_text.strip()
        if line_text:
            parts.append(line_text)
    return '\n'.join(parts)


def _choose_best_text(ocr_text: str, span_text: str) -> Tuple[str, bool]:
    """在 OCR 文本与 PyMuPDF span 文本之间选择更可靠的一个

    返回 (text, used_span_text)。
    - span 文本是数字版PDF的精确文本，优先使用
    - 但若 span 文本明显过短（region 匹配不全），退回 OCR
    """
    ocr_text = (ocr_text or '').strip()
    span_text = (span_text or '').strip()
    if not span_text:
        return ocr_text, False

    span_significant = len(re.sub(r'\s', '', span_text))
    ocr_significant = len(re.sub(r'\s', '', ocr_text))
    if ocr_significant == 0:
        return span_text, True
    # span 有效字符 >= OCR 的 60% 则可信（防止 span 截断）
    if span_significant >= ocr_significant * 0.6:
        return span_text, True
    return ocr_text, False


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
                font_name = span.get("font", "")
                # 加粗检测：
                # 1. PyMuPDF flags bit 16 (粗体)
                # 2. 字体名含 "Bold" / "Heavy" / "Black" / 中文 "粗" "黑" "加粗"
                is_bold = bool(flags & 16) or _is_bold_font_name(font_name)
                is_italic = bool(flags & 2) or 'italic' in font_name.lower() or '斜' in font_name
                spans.append({
                    'text': text,
                    'bbox': list(span.get("bbox", [0, 0, 0, 0])),
                    'font': font_name,
                    'size': float(span.get("size", 0)),
                    'flags': flags,
                    'is_bold': is_bold,
                    'is_italic': is_italic,
                })
    return spans


def _extract_page_text_blocks(page) -> List[Dict]:
    """用 PyMuPDF 提取段落级文本块（用于合并被版面分析切碎的片段）"""
    blocks = []
    try:
        for b in page.get_text("blocks"):
            if len(b) < 5:
                continue
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
            text = (text or '').strip()
            if not text:
                continue
            blocks.append({'bbox': [float(x0), float(y0), float(x1), float(y1)], 'text': text})
    except Exception:
        pass
    return blocks


def _is_bold_font_name(font_name: str) -> bool:
    """从字体名识别粗体

    例如：
    - "SimSun-Bold" / "SimHei" / "黑体" → True
    - "FangSong" / "仿宋" / "KaiTi" → False
    - "TimesNewRomanPS-BoldMT" → True
    """
    if not font_name:
        return False
    name = font_name.lower()
    bold_indicators = ['bold', 'heavy', 'black', 'demibold', 'semibold', 'mediumbold']
    cn_bold_indicators = ['粗', '黑', '加粗']
    if any(ind in name for ind in bold_indicators):
        return True
    if any(ind in font_name for ind in cn_bold_indicators):
        return True
    return False


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


def _union_bbox(a: List[float], b: List[float]) -> List[float]:
    """合并两个 bbox"""
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def _merge_elements_by_text_blocks(elements: List[ContentElement],
                                   blocks: List[Dict],
                                   page_width: float) -> List[ContentElement]:
    """根据 PyMuPDF 段落级文本块，合并被版面分析切碎的同行/同段文本片段

    PP-StructureV3 对数字版PDF经常过度切分：一个标题/段落被拆成
    "1"、"."、"招标条件" 等多个 block。PyMuPDF 的 get_text("blocks")
    能给出原始段落划分，因此把落在同一段落 block 里的碎片合并回
    一个 ContentElement，并用 block 的完整文本作为权威文本。
    """
    if not elements or not blocks:
        return elements

    def _match_block(elem: ContentElement):
        if not elem.bbox or len(elem.bbox) != 4:
            return None, 0.0
        ex1, ey1, ex2, ey2 = elem.bbox
        earea = (ex2 - ex1) * (ey2 - ey1)
        if earea <= 0:
            return None, 0.0
        best = None
        best_ratio = 0.0
        for blk in blocks:
            bx1, by1, bx2, by2 = blk['bbox']
            ox1, oy1 = max(ex1, bx1), max(ey1, by1)
            ox2, oy2 = min(ex2, bx2), min(ey2, by2)
            if ox1 < ox2 and oy1 < oy2:
                inter = (ox2 - ox1) * (oy2 - oy1)
                ratio = inter / earea
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = blk
        return best, best_ratio

    merged: List[ContentElement] = []
    current: Optional[ContentElement] = None
    current_block: Optional[Dict] = None
    acc_size = 0.0
    acc_weight = 0
    acc_bold = 0
    acc_italic = 0
    font_counter: Dict[Optional[str], int] = {}

    def _flush():
        nonlocal current, current_block, acc_size, acc_weight, acc_bold, acc_italic, font_counter
        if current is not None:
            if acc_weight > 0:
                current.font_size = acc_size / acc_weight
            current.is_bold = acc_bold > acc_weight / 2 if acc_weight else False
            current.is_italic = acc_italic > acc_weight / 2 if acc_weight else False
            if font_counter:
                current.font_name = max(font_counter, key=font_counter.get)
            current.alignment = _detect_alignment_from_bbox(current.bbox, page_width)
            merged.append(current)
        current = None
        current_block = None
        acc_size = 0.0
        acc_weight = 0
        acc_bold = 0
        acc_italic = 0
        font_counter = {}

    for elem in elements:
        if elem.type not in ('text', 'heading') or not elem.bbox:
            _flush()
            merged.append(elem)
            continue

        block, ratio = _match_block(elem)
        if not block or ratio < 0.5:
            _flush()
            merged.append(elem)
            continue

        w = len((elem.text or '').strip())
        if current_block is not None and current_block is block:
            # 同一 PyMuPDF 段落块：合并到当前元素
            current.bbox = _union_bbox(current.bbox, elem.bbox)
            current.text = block['text']  # 用 PyMuPDF 段落完整文本作为权威文本
            if elem.type == 'heading':
                current.type = 'heading'
            if elem.font_size and w > 0:
                acc_size += elem.font_size * w
                acc_weight += w
            if elem.is_bold:
                acc_bold += w
            if elem.is_italic:
                acc_italic += w
            if elem.font_name:
                font_counter[elem.font_name] = font_counter.get(elem.font_name, 0) + w
        else:
            _flush()
            current = ContentElement(
                type=elem.type,
                text=block['text'],
                bbox=elem.bbox[:],
                page_num=elem.page_num,
                font_name=elem.font_name,
                font_size=elem.font_size,
                is_bold=elem.is_bold,
                is_italic=elem.is_italic,
                alignment=_detect_alignment_from_bbox(elem.bbox, page_width),
            )
            current_block = block
            if elem.font_size and w > 0:
                acc_size = elem.font_size * w
                acc_weight = w
            else:
                acc_size = 0.0
                acc_weight = 0
            acc_bold = w if elem.is_bold else 0
            acc_italic = w if elem.is_italic else 0
            font_counter = {}
            if elem.font_name:
                font_counter[elem.font_name] = w

    _flush()
    return merged


def _detect_alignment(bbox, page_width, spans: List[Dict] = None,
                     text: str = '', block_label: str = None,
                     block: Dict = None) -> str:
    """综合判断对齐方式 —— 多信号投票

    信号优先级（从强到弱）：
    1. PP-StructureV3 block 自带 alignment（v3 字段最可靠）
    2. block bbox 几何（段落级，重要）
    3. span 检测（取第一行，抗 wrap 干扰）
    4. block_label 暗示（仅 doc_title 等明确类型）
    5. 文本特征规则（最保守，仅特殊模式）
    """
    # ---- 信号1：block 自带 alignment（最可靠）----
    block_align = _extract_block_alignment(block) if block else None
    if block_align:
        return block_align

    # ---- 信号2：block bbox 几何（段落级）----
    bbox_align = _detect_alignment_from_bbox(bbox, page_width)
    if bbox_align in ('center', 'right'):
        return bbox_align

    # ---- 信号3：span 检测（取第一行）----
    # 1 个 span 也能判（长标题），但只信任 center/right 结果
    if spans and len(spans) >= 1:
        span_align = _detect_alignment_from_spans(spans, page_width)
        if span_align in ('center', 'right'):
            return span_align

    # ---- 信号4：block_label 暗示（仅明确类型）----
    label_align = _align_from_label(block_label)
    if label_align:
        return label_align

    # ---- 信号5：文本特征（最保守）----
    feature_align = _detect_alignment_from_features(text, block_label)
    if feature_align:
        return feature_align

    return 'left'


def _get_pymupdf_block_bbox(spans: List[Dict]) -> Optional[List[float]]:
    """从 spans 算出 PyMuPDF 段落级 bbox（x1, y1, x2, y2）"""
    if not spans:
        return None
    try:
        x1 = min(s['bbox'][0] for s in spans)
        y1 = min(s['bbox'][1] for s in spans)
        x2 = max(s['bbox'][2] for s in spans)
        y2 = max(s['bbox'][3] for s in spans)
        return [x1, y1, x2, y2]
    except (KeyError, TypeError, ValueError):
        return None


def _detect_alignment_from_features(text: str, block_label: str = None) -> Optional[str]:
    """根据文本特征推断对齐方式

    谨慎的规则——只在很确定的情况下判 center/right：
    - block_label 是 doc_title/figure_title 等"已知居中类型" + 短文本 → center
    - 落款日期（YYYY年M月D日）+ 文本很短（<= 12字符）→ right（落款常用）
    - 不再使用"句末标点"等过激规则
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    # 规则1：仅当 block_label 明确是"标题型"时，短文本才判 center
    if block_label:
        label_lower = block_label.lower()
        if label_lower in ('doc_title', 'figure_title', 'table_title',
                            'reference_title', 'algorithm_title'):
            if len(text) <= 30:  # 标题都比较短
                return 'center'

    # 规则2：纯数字独立成段（页码）→ center
    if re.match(r'^\d{1,4}$', text):
        return 'center'

    # 规则3：落款日期（短，整行）→ right
    if re.match(r'^\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$', text) and len(text) <= 12:
        return 'right'
    if re.match(r'^[一二三四五六七八九十0-9]{4}年[一二三四五六七八九十0-9]{1,2}月[一二三四五六七八九十0-9]{1,2}日$', text):
        return 'right'

    # 其他情况不强行判，返回 None 让 bbox/span 接管
    return None


def _align_from_label(block_label: str = None) -> Optional[str]:
    """根据 block_label 推断对齐

    只在结构明确时判 center：
    - doc_title / figure_title / table_title / reference_title / algorithm_title
    其他 label（包括 paragraph_title）不强行判（避免把正文误判居中）
    """
    if not block_label:
        return None
    label_lower = block_label.lower()
    # title/doc_title：封面大标题，强制居中
    if label_lower in ('title', 'doc_title', 'figure_title', 'table_title',
                        'reference_title', 'algorithm_title'):
        return 'center'
    return None


def _detect_alignment_from_bbox(bbox, page_width) -> str:
    """block bbox 几何检测（段落级，比 span 稳定）

    关键改进：
    - 居中要求：左右 margin 接近 + 段落宽度 < 85% 页宽
    - 但还要"两侧都明显大于页边距"才判 center（否则是左对齐段落）
    - 右对齐要求：右 margin 极小 + 左 margin 显著 > 0
    - 段落宽度 >= 90% 页宽 → 几乎都是左对齐
    """
    x1, y1, x2, y2 = bbox
    left = x1
    right = page_width - x2
    elem_width = x2 - x1

    # 容差：左右 margin 相差 < 5% 页宽 算居中
    tol = page_width * 0.05

    # 如果段落几乎横跨整个页面 → 左对齐（margin 是页边距，不是"文字居中"）
    if elem_width > page_width * 0.9:
        return 'left'

    # 居中：左右 margin 接近 + 两侧 margin 都明显大于页边距（> 25% 页宽）
    # 关键：左对齐段落的 left 就是页边距（通常 75-110pt = 13-18% 页宽）
    # 所以要求两侧 margin 都 > 25% 页宽（150pt）才判 center
    if (abs(left - right) < tol
        and elem_width < page_width * 0.85
        and left > page_width * 0.25
        and right > page_width * 0.25):
        return 'center'

    # 右对齐：右边贴页边距，左边留大空白
    if right < tol and left > tol:
        return 'right'

    return 'left'


def _detect_alignment_from_spans(spans: List[Dict], page_width: float) -> str:
    """用 span 实际位置判断对齐（更精确）

    关键算法：
    1. 把 spans 按 y 坐标分组（行）
    2. 取**第一行**的左 margin 来判定（最后一行 wrap 短行不可信）
    3. 也用所有 span 整体 left/right 兜底
    """
    if not spans:
        return 'left'

    # 按 y 坐标排序（PDF 坐标 y 向下递增）
    sorted_spans = sorted(spans, key=lambda s: (round(s['bbox'][1], 1), s['bbox'][0]))

    # 第一行的 spans（y 坐标最小）
    first_y = round(sorted_spans[0]['bbox'][1], 1)
    first_line_spans = [s for s in sorted_spans if round(s['bbox'][1], 1) == first_y]

    if first_line_spans:
        first_left = min(s['bbox'][0] for s in first_line_spans)
        first_right = page_width - max(s['bbox'][2] for s in first_line_spans)
        # 容差：5% 页宽 = 约 30pt
        tol = page_width * 0.05
        # 第一行左边贴近左页边距（< 20% 页宽）→ 左对齐
        # 0.2 容差考虑段落有左缩进（1-2 字符 = 24-48pt）
        if first_left < page_width * 0.20:
            return 'left'
        # 第一行右边贴页边距 → 右对齐
        if first_right < page_width * 0.08:
            return 'right'
        # 第一行左右 margin 都大 → 居中
        if abs(first_left - first_right) < tol:
            return 'center'

    # 兜底：用所有 spans 整体判定
    lefts = [s['bbox'][0] for s in spans]
    rights = [s['bbox'][2] for s in spans]
    min_left = min(lefts)
    max_right = max(rights)
    left_margin = min_left
    right_margin = page_width - max_right
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
# 数字版PDF专用提取路径（纯PyMuPDF，不经OCR）
#
# 适用：内容流含真实文本的PDF（Word/WPS导出等）。
# 核心思路：
# 1. 按 baseline（文字基线）把所有 span 聚成"视觉行"——解决
#    中英混排/异字号被拆成多段的问题（如"2026年7月"拆成"20267"+"年 月"）
# 2. 行内按 x 排序拼接，按间隙插入空格——保证"CAB-A"、网址等行内片段不脱离正文
# 3. 按"上一行是否排满 + 当前行起点 + 行距 + 样式"把视觉行合并成逻辑段落
# 4. 识别目录行（标题+点线引导符+页码），用制表位重建
# 5. PyMuPDF find_tables 检测表格，表格内文字不进入正文流
# ============================================================

# CJK字符判定（汉字/中文标点/全角符号）
_CJK_CHAR_RE = re.compile(
    '[\u2e80-\u2eff\u3000-\u303f\u31c0-\u31ef\u3200-\u32ff'
    '\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]')
_LATIN_WORD_RE = re.compile('[A-Za-z0-9]')

# 目录引导符
_TOC_DOT_CHARS = '.\uff0e\u00b7\u2022\u2026\u22ef'


def _is_cjk_char(ch: str) -> bool:
    return bool(_CJK_CHAR_RE.match(ch)) if ch else False


# 配对括号/引号（用于判断跨行换行）
_PAIR_OPEN = '（【「『《〈“‘([{'
_PAIR_CLOSE = '）】」』》〉”’)]}'


def _has_unclosed_pair(text: str) -> bool:
    """文本中是否存在未闭合的开括号/引号（行尾跨行的强信号）"""
    depth = 0
    for ch in text:
        if ch in _PAIR_OPEN:
            depth += 1
        elif ch in _PAIR_CLOSE and depth > 0:
            depth -= 1
    return depth > 0


def _dp_collect_spans(page) -> List[Dict]:
    """收集一页内所有水平文本span（含baseline、颜色等完整信息）

    使用rawdict取字符级信息：PDF里的窄空格（宽度<0.35em）多为
    Word/WPS"自动调整中英文间距"导出的产物，予以去除（生成Word时
    Word会按自身设置重新渲染自动间距）；真实输入的空格（较宽）保留。
    """
    spans = []
    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            # 跳过旋转/竖排文本
            d = line.get("dir") or (1, 0)
            if abs(d[0]) < 0.98 or abs(d[1]) > 0.2:
                continue
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                if not chars:
                    continue
                text, bbox, origin = _dp_clean_span_chars(
                    chars, float(span.get("size", 0)),
                    tuple(span.get("origin", (0, 0))))
                if not text.strip():
                    continue
                flags = span.get("flags", 0)
                font_name = span.get("font", "")
                spans.append({
                    'text': text,
                    'bbox': bbox,
                    'origin': origin,
                    'font': font_name,
                    'size': float(span.get("size", 0)),
                    'flags': flags,
                    'color': int(span.get("color", 0)),
                    'is_bold': bool(flags & 16) or _is_bold_font_name(font_name),
                    'is_italic': bool(flags & 2) or 'italic' in font_name.lower(),
                    'is_superscript': bool(flags & 1),
                })
    return spans


def _dp_clean_span_chars(chars: List[Dict], size: float, origin: Tuple[float, float]):
    """字符级清洗span：去自动间距伪空格、补偿字距过大的西文单词空格

    返回 (text, bbox, origin)：
    - 窄空格（<0.35em）且相邻CJK：去除（Word自动间距产物）
    - 字符间隙过大（>0.5em）且两侧均为西文字母数字：补空格
    - bbox/origin按保留字符重算
    """
    kept: List[Dict] = []
    n = len(chars)
    for i, c in enumerate(chars):
        ch = c.get("c", "")
        if ch in (" ", "\t"):
            w = c["bbox"][2] - c["bbox"][0]
            if size > 0 and w < 0.35 * size:
                # 前一个非空字符 / 后一个非空字符
                prev_ch = kept[-1]["c"] if kept else ""
                next_ch = ""
                for j in range(i + 1, n):
                    if chars[j].get("c", "") not in (" ", "\t"):
                        next_ch = chars[j]["c"]
                        break
                if _is_cjk_char(prev_ch) or _is_cjk_char(next_ch):
                    continue  # 丢弃伪空格
        kept.append(c)
    if not kept:
        return "", [0, 0, 0, 0], origin
    # 西文单词间隙补偿（内容流未写空格、仅用字距分隔的情形）
    pieces: List[str] = []
    prev_c = None
    for c in kept:
        if prev_c is not None:
            gap = c["bbox"][0] - prev_c["bbox"][2]
            if (size > 0 and gap > 0.5 * size
                    and _LATIN_WORD_RE.match(prev_c["c"] or "")
                    and _LATIN_WORD_RE.match(c["c"] or "")):
                pieces.append(" ")
        pieces.append(c["c"])
        prev_c = c
    x0 = min(c["bbox"][0] for c in kept)
    x1 = max(c["bbox"][2] for c in kept)
    y0 = min(c["bbox"][1] for c in kept)
    y1 = max(c["bbox"][3] for c in kept)
    o = (kept[0]["origin"][0], origin[1]) if kept[0].get("origin") else origin
    return "".join(pieces), [x0, y0, x1, y1], o


def _dp_group_rows(spans: List[Dict]) -> List[Dict]:
    """按baseline把span聚成视觉行（同行合并，核心防碎片化步骤）

    同一基线的不同字体/字号span（如 Times数字 + 宋体汉字）合并为一行。
    容差 max(2pt, 0.35*字号)：覆盖生成器基线误差，又远小于行距。
    """
    if not spans:
        return []
    rows: List[Dict] = []

    def _row_tol(row, span):
        return max(2.0, 0.35 * max(row['size'], span['size']))

    for span in sorted(spans, key=lambda s: (s['origin'][1], s['bbox'][0])):
        placed = False
        for row in rows:
            if abs(span['origin'][1] - row['baseline']) <= _row_tol(row, span):
                row['spans'].append(span)
                # 行的主字号取"字符数最多"的span，行内异体字号保留在run里
                if len(span['text']) > row.get('_dom_len', 0):
                    row['size'] = span['size']
                    row['_dom_len'] = len(span['text'])
                placed = True
                break
        if not placed:
            rows.append({
                'baseline': span['origin'][1],
                'spans': [span],
                'size': span['size'],
                '_dom_len': len(span['text']),
            })

    # 后处理：把"孤立小片段行"（上标/下标等）并回相邻主行
    rows = _dp_merge_orphan_fragments(rows)

    # 行内排序并计算行bbox/文本/runs
    result = []
    for row in rows:
        row['spans'].sort(key=lambda s: s['bbox'][0])
        xs0 = min(s['bbox'][0] for s in row['spans'])
        xs1 = max(s['bbox'][2] for s in row['spans'])
        ys0 = min(s['bbox'][1] for s in row['spans'])
        ys1 = max(s['bbox'][3] for s in row['spans'])
        row['x0'], row['x1'] = xs0, xs1
        row['y0'], row['y1'] = ys0, ys1
        row['text'], row['runs'] = _dp_build_row_text(row['spans'])
        if not row['text']:
            continue
        # 行级样式聚合（run级样式已保留，这里给段落判断用）
        chars = {}
        for s in row['spans']:
            k = (s['font'], s['is_bold'], s['is_italic'], s['color'])
            chars[k] = chars.get(k, 0) + len(s['text'].strip())
        dom = max(chars, key=chars.get)
        row['font'], row['bold'], row['italic'], row['color'] = dom
        result.append(row)
    result.sort(key=lambda r: (r['baseline'], r['x0']))
    return result


def _dp_merge_orphan_fragments(rows: List[Dict]) -> List[Dict]:
    """把疑似上标/下标的孤立短行并回主行

    特征：片段极短、基线与主行偏差在 (容差, 0.9*字号] 之间、水平范围有重叠。
    """
    if len(rows) < 2:
        return rows
    merged_flags = [False] * len(rows)
    out = []
    for i, row in enumerate(rows):
        if merged_flags[i]:
            continue
        frag_text = ''.join(s['text'] for s in row['spans']).strip()
        # 主行自身处理
        # 尝试把相邻行(前后)的孤立短片段吸收进来
        for j in range(len(rows)):
            if j == i or merged_flags[j]:
                continue
            other = rows[j]
            other_text = ''.join(s['text'] for s in other['spans']).strip()
            if len(other_text) > 6:
                continue
            if _dp_rows_x_overlap(row, other) <= 0:
                continue
            bl_diff = other['baseline'] - row['baseline']
            max_off = 0.9 * max(row['size'], other['size'])
            if 2.0 < abs(bl_diff) <= max_off:
                row['spans'].extend(other['spans'])
                merged_flags[j] = True
        out.append(row)
    return out


def _dp_rows_x_overlap(a: Dict, b: Dict) -> float:
    """两行水平投影重叠长度"""
    ax0, ax1 = min(s['bbox'][0] for s in a['spans']), max(s['bbox'][2] for s in a['spans'])
    bx0, bx1 = min(s['bbox'][0] for s in b['spans']), max(s['bbox'][2] for s in b['spans'])
    return min(ax1, bx1) - max(ax0, bx0)


def _dp_build_row_text(spans: List[Dict]):
    """行内span按x排序拼接成文本 + run列表（保留行内异体样式）

    - 间隙超过阈值且边界非CJK-CJK时插入空格
    - 相邻同样式span合并为一个run
    """
    runs = []
    texts = []
    prev = None
    for s in spans:
        piece = s['text']
        if prev is not None:
            gap = s['bbox'][0] - prev['bbox'][2]
            need_space = False
            if gap > max(1.2, 0.5 * prev['size']):
                a, b = prev['text'][-1], piece[:1]
                # CJK-CJK边界不插空格（两端对齐的字间距不是真空格）
                if not (_is_cjk_char(a) and _is_cjk_char(b)):
                    need_space = True
            if need_space and not prev['text'].endswith(' ') and not piece.startswith(' '):
                texts.append(' ')
                if runs:
                    runs[-1]['text'] += ' '
        texts.append(piece)
        style_key = (round(s['size'], 1), s['is_bold'], s['is_italic'], s['color'])
        if runs and runs[-1]['key'] == style_key:
            runs[-1]['text'] += piece
        else:
            runs.append({
                'key': style_key,
                'text': piece,
                'size': s['size'],
                'bold': s['is_bold'],
                'italic': s['is_italic'],
                'color': s['color'],
            })
        prev = s
    text = ''.join(texts).strip()
    # 去掉首尾run的空白
    while runs and not runs[0]['text'].strip():
        runs.pop(0)
    while runs and not runs[-1]['text'].strip():
        runs.pop()
    if runs:
        runs[0]['text'] = runs[0]['text'].lstrip()
        runs[-1]['text'] = runs[-1]['text'].rstrip()
    clean_runs = [r for r in runs if r['text']]
    return text, clean_runs


def _dp_join_lines(a_text: str, b_text: str) -> str:
    """段落内两行拼接：仅在西文单词边界补空格，其余直接拼接"""
    if not a_text:
        return b_text
    if not b_text:
        return a_text
    ca, cb = a_text[-1], b_text[0]
    if _LATIN_WORD_RE.match(ca) and _LATIN_WORD_RE.match(cb):
        return a_text + ' ' + b_text
    return a_text + b_text


def _dp_match_toc(text: str):
    """识别目录条目：标题 + 引导点线 + 页码

    返回 (title, page_no) 或 None
    """
    if not text or len(text) > 200:
        return None
    # 点线之间的空白去掉（"… … …"归一为"………"）
    t = re.sub(r'(?<=[' + re.escape(_TOC_DOT_CHARS) + r'])\s+'
               r'(?=[' + re.escape(_TOC_DOT_CHARS) + r'])', '', text)
    m = re.search(r'(?:[\.\uff0e\u00b7\u2022]{3,}|[\u2026\u22ef]{2,})\s*(\d{1,4})\s*$', t)
    if not m:
        return None
    title = t[:m.start()].rstrip(' ' + _TOC_DOT_CHARS).strip()
    if not title or len(title) > 60:
        return None
    return title, m.group(1)


def _dp_is_page_numberish(text: str) -> bool:
    t = text.strip()
    if re.fullmatch(r'-?\s*[\dIVXLCDMivxlcdm]+\s*-?', t):
        return True
    if re.fullmatch(r'第\s*[\d零一二三四五六七八九十百千]+\s*页(\s*共\s*[\d零一二三四五六七八九十百千]+\s*页)?', t):
        return True
    if re.fullmatch(r'[\d]+\s*/\s*[\d]+', t):
        return True
    if re.fullmatch(r'[IVXLC]+', t):
        return True
    return False


def _dp_extract_tables(page, page_num: int, is_landscape: bool,
                       spans: List[Dict]) -> Tuple[List[Dict], List[List[float]]]:
    """检测页面表格：返回 (table元素列表, 有效表格bbox列表)

    表格bbox用于把表格内文字从正文流中排除。
    单元格文本用页面已清洗span按基线重建（find_tables的extract()
    按字距阈值补空格，两端对齐单元格会产生"名 称"这类伪空格）。
    """
    table_elems = []
    valid_bboxes = []
    try:
        finder = page.find_tables()
    except Exception:
        return [], []
    for t in finder.tables:
        rows = _dp_table_rows_from_spans(t, spans)
        if not rows:
            continue
        non_empty = sum(1 for r in rows for c in r if c and str(c).strip())
        if t.row_count < 2 or t.col_count < 2 or non_empty < 4:
            continue
        bb = list(t.bbox)
        if len(bb) != 4:
            continue
        table_elems.append(ContentElement(
            type='table', text='', bbox=bb, page_num=page_num,
            html=_rows_to_html(rows), is_landscape=is_landscape,
        ))
        valid_bboxes.append(bb)
    return table_elems, valid_bboxes


def _dp_table_rows_from_spans(t, spans: List[Dict]) -> List[List[str]]:
    """用页面已清洗span重建表格单元格文本

    - 按单元格bbox圈选中心点落入的span
    - 以基线重组视觉行（"名"+"称：..."这类被内容流拆开的同行片段
      会正确合并，且CJK边界不补空格）
    - 单元格内多行用'\\n'保留（python-docx渲染为软换行）
    - 合并格（bbox为None）输出''
    """
    rows_out: List[List[str]] = []
    for row in t.rows:
        cells_out: List[str] = []
        for cb in row.cells:
            if cb is None:
                cells_out.append('')
                continue
            x0, y0, x1, y1 = cb
            sel = [s for s in spans
                   if x0 <= (s['bbox'][0] + s['bbox'][2]) / 2 <= x1
                   and y0 <= (s['bbox'][1] + s['bbox'][3]) / 2 <= y1]
            if sel:
                lines = [r['text'] for r in _dp_group_rows(sel) if r['text']]
                cells_out.append('\n'.join(lines))
            else:
                cells_out.append('')
        rows_out.append(cells_out)
    return rows_out


def _dp_span_in_tables(span: Dict, table_bboxes: List[List[float]]) -> bool:
    """span中心是否落在某个表格bbox内"""
    cx = (span['bbox'][0] + span['bbox'][2]) / 2
    cy = (span['bbox'][1] + span['bbox'][3]) / 2
    for bb in table_bboxes:
        if bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
            return True
    return False


def _dp_detect_alignment(rows: List[Dict], content_left: float, content_right: float,
                         page_width: float) -> str:
    """段落对齐检测（基于几何）

    - center：探针行（末行优先）左右留白均衡 + 全部行的中心点互相靠近
      （正文两端对齐行左右留白也对称，但末行必始于左边界，用lm阈值排除：
      首行缩进恰为2字符=2*字号，真实居中行的留白>=2.5*字号）
    - right：末行右缘贴齐内容右边界且首行明显缩进
    - 其余 left
    """
    if not rows:
        return 'left'
    full_tol = 0.04 * page_width
    right_edge = content_right - full_tol
    partial = [r for r in rows if r['x1'] < right_edge]
    # 探针行：末行（最能反映对齐方式），末行排满则取最窄的不满行
    if rows[-1]['x1'] < right_edge:
        probe = rows[-1]
    elif partial:
        probe = min(partial, key=lambda r: r['x1'] - r['x0'])
    else:
        probe = min(rows, key=lambda r: r['x1'] - r['x0'])
    lm = probe['x0'] - content_left
    rm = content_right - probe['x1']
    # 居中段落各行中心点互相靠近（正文末行偏左、右对齐各行中心发散）
    centers = [(r['x0'] + r['x1']) / 2 for r in rows]
    centers_aligned = max(centers) - min(centers) < 0.03 * page_width
    if (centers_aligned
            and abs(lm - rm) < 0.025 * page_width
            and lm > 2.5 * max(probe['size'], 1)):
        return 'center'
    # 右对齐：末行贴右边界 + 首行左缘离内容左边界较远
    last = rows[-1]
    first = rows[0]
    if (last['x1'] >= right_edge
            and first['x0'] - content_left > 0.08 * page_width):
        return 'right'
    return 'left'


def _dp_rows_to_elements(rows: List[Dict], page_num: int, page_width: float,
                         is_landscape: bool) -> List[ContentElement]:
    """把一页的视觉行组装成段落级ContentElement"""
    if not rows:
        return []
    content_left = min(r['x0'] for r in rows)
    content_right = max(r['x1'] for r in rows)
    content_width = content_right - content_left
    body_size = _dp_dominant_size(rows)

    elements: List[ContentElement] = []
    para_rows: List[Dict] = []  # 当前段落

    def _flush():
        nonlocal para_rows
        if not para_rows:
            return
        rows_ = para_rows
        para_rows = []
        text = ''
        runs = []
        for i, r in enumerate(rows_):
            if i == 0:
                text = r['text']
                runs = [dict(x) for x in r['runs']]
            else:
                text = _dp_join_lines(text, r['text'])
                if runs and rows_[i - 1]['runs']:
                    # 行边界拼接规则（西文补空格）
                    a = rows_[i - 1]['runs'][-1]['text']
                    b = r['runs'][0]['text'] if r['runs'] else ''
                    if a and b and _LATIN_WORD_RE.match(a[-1]) and _LATIN_WORD_RE.match(b[0]):
                        runs[-1]['text'] += ' '
                        # b的开头不需要加空格（拼接进下一run）
                for x in r['runs']:
                    runs.append(dict(x))
        bbox = [min(r['x0'] for r in rows_), min(r['y0'] for r in rows_),
                max(r['x1'] for r in rows_), max(r['y1'] for r in rows_)]
        # 段落样式：以字符数最多的行为准
        dom_row = max(rows_, key=lambda r: len(r['text']))
        alignment = _dp_detect_alignment(rows_, content_left, content_right, page_width)
        # 目录条目
        toc = _dp_match_toc(rows_[0]['text']) if len(rows_) == 1 else None
        if toc:
            elements.append(ContentElement(
                type='toc', text=rows_[0]['text'], bbox=bbox, page_num=page_num,
                font_name=dom_row['font'], font_size=dom_row['size'],
                is_bold=dom_row['bold'], is_italic=dom_row['italic'],
                color=dom_row['color'], alignment='left',
                runs=[dict(x) for x in rows_[0]['runs']],
                is_toc=True, toc_title=toc[0], toc_page_no=toc[1],
                left_indent_pt=max(0.0, rows_[0]['x0'] - content_left),
                is_landscape=is_landscape,
            ))
            return
        elements.append(ContentElement(
            type='text', text=text, bbox=bbox, page_num=page_num,
            font_name=dom_row['font'], font_size=dom_row['size'],
            is_bold=dom_row['bold'], is_italic=dom_row['italic'],
            color=dom_row['color'], alignment=alignment,
            runs=runs,
            left_indent_pt=max(0.0, rows_[0]['x0'] - content_left),
            is_landscape=is_landscape,
        ))

    for row in rows:
        toc = _dp_match_toc(row['text'])
        if toc:
            _flush()
            para_rows = [row]
            _flush()
            continue

        if para_rows:
            prev = para_rows[-1]
            gap = row['baseline'] - prev['baseline']
            line_ok = gap <= 2.3 * max(prev['size'], row['size'], body_size)
            style_ok = (abs(row['size'] - prev['size']) <= 1.2
                        and row['bold'] == prev['bold'])
            # 上一行是否"排满"（右缘接近内容右边界）
            prev_full = prev['x1'] >= content_right - 0.04 * page_width
            # 当前行起点：回到正文左边界 = 续行；缩进1.2~3.5字符 = 首行/悬挂
            start_offset = row['x0'] - content_left
            cont_flush = start_offset <= 0.8 * max(row['size'], 1)
            cont_hanging = (1.2 * row['size'] < start_offset <= 3.5 * row['size']
                            and not prev['text'].endswith(('。', '！', '？', '；')))
            # 居中续行（封面大标题等跨行居中文本）：前行排满、未以闭合标点收尾、
            # 前行有未闭合的开括号/引号（换行强信号）或当前行很短且居中
            row_is_center = _dp_detect_alignment(
                [row], content_left, content_right, page_width) == 'center'
            cont_center = False
            if (prev_full and row_is_center
                    and not prev['text'].endswith(
                        ('。', '！', '？', '；', '：', '）', '】', '」', '\u201d'))):
                row_center = (row['x0'] + row['x1']) / 2
                prev_center = (prev['x0'] + prev['x1']) / 2
                centers_ok = abs(row_center - prev_center) < 0.05 * page_width
                prev_open = _has_unclosed_pair(prev['text'])
                short_row = (row['x1'] - row['x0']) < 0.25 * (content_right - content_left)
                if (start_offset > 0.8 * max(row['size'], 1)
                        and centers_ok and (prev_open or short_row)):
                    cont_center = True
            # 普通续行不能是居中行（避免把居中段落误并入左对齐正文）
            merge_ok = False
            if line_ok and style_ok and prev_full:
                if cont_center:
                    merge_ok = True
                elif (cont_flush or cont_hanging) and not row_is_center:
                    merge_ok = True
            if merge_ok:
                para_rows.append(row)
                continue
        _flush()
        para_rows = [row]
    _flush()
    return elements


def _dp_dominant_size(rows: List[Dict]) -> float:
    """一页的主字号（按字符数加权）"""
    counter: Dict[float, int] = {}
    for r in rows:
        counter[r['size']] = counter.get(r['size'], 0) + len(r['text'])
    if not counter:
        return 10.5
    return max(counter, key=counter.get)


def is_digital_pdf(pdf_path: str, sample_pages: int = 10, min_chars: int = 30) -> bool:
    """判断是否为数字版PDF（有真实文本层），是则走纯PyMuPDF路径"""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return False
    try:
        n = len(doc)
        if n == 0:
            return False
        step = max(1, n // sample_pages)
        idxs = list(range(0, n, step))[:sample_pages]
        with_text = 0
        for i in idxs:
            try:
                if len(doc.load_page(i).get_text("text").strip()) >= min_chars:
                    with_text += 1
            except Exception:
                pass
        return with_text >= max(1, int(len(idxs) * 0.6))
    except Exception:
        return False
    finally:
        doc.close()


def extract_digital_pdf(pdf_path: str) -> List[ContentElement]:
    """数字版PDF主提取入口（纯PyMuPDF，速度快且无OCR误差）"""
    doc = fitz.open(pdf_path)
    elements: List[ContentElement] = []
    total_pages = len(doc)
    from config import MAX_PAGES
    if MAX_PAGES and MAX_PAGES > 0 and total_pages > MAX_PAGES:
        total_pages = MAX_PAGES

    # 第一遍：逐页收集行/表格（先不过滤页眉页脚，便于统计重复）
    page_rows: List[List[Dict]] = []
    page_tables: List[List[ContentElement]] = []
    page_meta = []
    for pno in range(total_pages):
        page = doc.load_page(pno)
        is_landscape = page.rect.width > page.rect.height
        spans = _dp_collect_spans(page)
        table_elems, table_bboxes = _dp_extract_tables(page, pno, is_landscape, spans)
        spans = [s for s in spans
                 if not _dp_span_in_tables(s, table_bboxes)]
        rows = _dp_group_rows(spans)
        page_rows.append(rows)
        page_tables.append(table_elems)
        page_meta.append({'width': page.rect.width, 'height': page.rect.height,
                          'landscape': is_landscape})
    doc.close()

    # 页眉页脚：跨页重复的条带内短文本 + 页码样式
    band_text_count: Dict[str, int] = {}
    for pno, rows in enumerate(page_rows):
        h = page_meta[pno]['height']
        for r in rows:
            if r['y0'] < 50 or r['y1'] > h - 55:
                key = r['text'].strip()
                if len(key) > 6:
                    band_text_count[key] = band_text_count.get(key, 0) + 1
    repeat_threshold = max(3, int(total_pages * 0.3))

    def _is_header_footer(pno: int, row: Dict) -> bool:
        h = page_meta[pno]['height']
        in_band = row['y0'] < 50 or row['y1'] > h - 55
        t = row['text'].strip()
        if not in_band:
            # 页码可能落在较宽的底部条带外沿（如罗马页码"I/II"），
            # 纯页码样式的短文本直接剔除
            if row['y1'] > h - 95 and len(t) <= 8 and _dp_is_page_numberish(t):
                return True
            return False
        if _dp_is_page_numberish(t):
            return True
        if len(t) <= 8:
            return True
        if band_text_count.get(t, 0) >= repeat_threshold:
            return True
        return False

    # 第二遍：过滤页眉页脚并组装段落
    for pno, rows in enumerate(page_rows):
        meta = page_meta[pno]
        kept = [r for r in rows if not _is_header_footer(pno, r)]
        elements.extend(_dp_rows_to_elements(kept, pno, meta['width'], meta['landscape']))
        elements.extend(page_tables[pno])

    elements.sort(key=lambda e: (e.page_num,
                                  e.bbox[1] if e.bbox else 0,
                                  e.bbox[0] if e.bbox else 0))
    return elements


# ============================================================
# 兼容旧接口
# ============================================================
def extract_native_pdf(pdf_path: str) -> List[ContentElement]:
    return extract_with_layout_analysis(pdf_path, dpi=200)


def extract_scanned_pdf(pdf_path: str) -> List[ContentElement]:
    return extract_with_layout_analysis(pdf_path, dpi=300)
