# -*- coding: utf-8 -*-
"""Word文档生成模块 - 按照固定格式规范生成Word文档

格式规范：
- 页面：A4，上下2.54cm，左右3.18cm，页眉1.5cm，页脚1.75cm
- 正文：宋体+Times New Roman，小四号(12pt)，1.5倍行距，首行缩进2字符
- 标题：宋体加粗，一级三号(16pt)/二级小三号(15pt)/三级四号(14pt)/四级+小四号(12pt)
- 表格：居中，行高0.75cm，表内五号(10.5pt)单倍行距，表名小四加粗居中在上方
- 图片：居中嵌入型，图名居中在下方，分章编号
"""

import re
import io
from typing import List

from docx import Document
from docx.shared import Pt, Cm, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

from config import (
    PAGE_SIZE, PAGE_MARGINS, HEADER_FOOTER_DISTANCE,
    HEADING_SIZES, HEADING_FONT, BODY_FONT,
    TABLE_CONFIG, IMAGE_CONFIG, USABLE_WIDTH_CM,
)
from pdf_extractor import ContentElement
from style_mapper import FigureTableCounter


# ============================================================
# 页眉页脚检测
# ============================================================
_HEADER_FOOTER_RE = re.compile(
    r'^(第\s*\d+\s*页|'
    r'-\s*\d+\s*-|'                  # - 5 -
    r'第\s*[零一二三四五六七八九十百千0-9]+\s*页|'
    r'Page\s*\d+|'                   # Page 3
    r'\d+\s*/\s*\d+\s*$|'            # 3/10
    r'[\-—]\s*\d+\s*[\-—]\s*$)'      # -3- / —3—
)


def _is_likely_header_footer(text: str) -> bool:
    """判断文本是否像页眉页脚

    规则：
    1. 长度 <= 20 字符
    2. 匹配"第N页"/"-3-"/"Page 3"/"3/10"等页码模式
    3. 全是数字或"-"的组合
    """
    text = text.strip()
    if len(text) > 20:
        return False
    if _HEADER_FOOTER_RE.match(text):
        return True
    # 纯数字/罗马数字短文本
    if len(text) <= 6 and re.fullmatch(r'[\dIVXLCDM\-—/、\s]+', text):
        return True
    return False


# ============================================================
# 底层XML操作工具
# ============================================================
def _set_run_font(run, cn_font='宋体', en_font='Times New Roman',
                  size_pt=12, bold=False, italic=False):
    """设置Run的字体，正确处理中文（w:eastAsia）"""
    run.font.name = en_font
    run.font.size = Pt(size_pt)
    run.bold = bold
    run.italic = italic
    # 设置中文字体
    r = run._element
    rPr = r.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.insert(0, rFonts)
    rFonts.set(qn('w:eastAsia'), cn_font)


def _set_paragraph_spacing(paragraph, line_spacing=1.5,
                            space_before=0, space_after=0,
                            snap_to_grid=False):
    """设置段落间距：行距、段前段后、网格对齐"""
    pf = paragraph.paragraph_format
    pf.line_spacing = line_spacing
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)

    # 取消"如果定义了文档网格，则对齐到网格"
    if not snap_to_grid:
        pPr = paragraph._element.get_or_add_pPr()
        snapToGrid = pPr.find(qn('w:snapToGrid'))
        if snapToGrid is None:
            snapToGrid = OxmlElement('w:snapToGrid')
            pPr.append(snapToGrid)
        snapToGrid.set(qn('w:val'), '0')


def _set_row_height(row, height_cm, rule='atLeast'):
    """设置表格行高"""
    tr = row._tr
    trPr = tr.get_or_add_trPr()
    trHeight = OxmlElement('w:trHeight')
    # 1cm = 567 twips
    trHeight.set(qn('w:val'), str(int(height_cm * 567)))
    trHeight.set(qn('w:hRule'), rule)
    trPr.append(trHeight)


def _set_repeat_header(row):
    """设置表格第一行为重复标题行（跨页时重复）"""
    tr = row._tr
    trPr = tr.get_or_add_trPr()
    tblHeader = OxmlElement('w:tblHeader')
    tblHeader.set(qn('w:val'), 'true')
    trPr.append(tblHeader)


def _set_cant_split(row):
    """设置表格行不可跨页拆分"""
    tr = row._tr
    trPr = tr.get_or_add_trPr()
    cantSplit = OxmlElement('w:cantSplit')
    cantSplit.set(qn('w:val'), 'true')
    trPr.append(cantSplit)


def _set_table_borders(table):
    """设置表格边框（黑色实线）"""
    tbl = table._tbl
    tblPr = tbl.tblPr
    borders = OxmlElement('w:tblBorders')
    for border_name in ['top', 'left', 'bottom', 'right', 'insideH', 'insideV']:
        border = OxmlElement(f'w:{border_name}')
        border.set(qn('w:val'), 'single')
        border.set(qn('w:sz'), '4')  # 0.5pt
        border.set(qn('w:space'), '0')
        border.set(qn('w:color'), '000000')
        borders.append(border)
    tblPr.append(borders)


# ============================================================
# 页面设置
# ============================================================
def setup_page(doc: Document):
    """设置A4页面、页边距、页眉页脚距离"""
    section = doc.sections[0]
    section.page_width = Cm(PAGE_SIZE['width_cm'])
    section.page_height = Cm(PAGE_SIZE['height_cm'])
    section.top_margin = Cm(PAGE_MARGINS['top_cm'])
    section.bottom_margin = Cm(PAGE_MARGINS['bottom_cm'])
    section.left_margin = Cm(PAGE_MARGINS['left_cm'])
    section.right_margin = Cm(PAGE_MARGINS['right_cm'])
    section.header_distance = Cm(HEADER_FOOTER_DISTANCE['header_cm'])
    section.footer_distance = Cm(HEADER_FOOTER_DISTANCE['footer_cm'])


# ============================================================
# 正文段落
# ============================================================
def add_body_paragraph(doc: Document, text: str, size_pt: float = None,
                        alignment: str = 'left', bold: bool = False):
    """添加正文段落

    宋体+Times New Roman，小四号(12pt)或指定字号，1.5倍行距，首行缩进2字符
    """
    p = doc.add_paragraph()
    run = p.add_run(text)
    actual_size = size_pt if size_pt and size_pt > 0 else BODY_FONT['size_pt']
    _set_run_font(run,
                  cn_font=BODY_FONT['cn'],
                  en_font=BODY_FONT['en'],
                  size_pt=actual_size,
                  bold=bold)
    _set_paragraph_spacing(p,
                           line_spacing=BODY_FONT['line_spacing'],
                           space_before=BODY_FONT['space_before_pt'],
                           space_after=BODY_FONT['space_after_pt'],
                           snap_to_grid=BODY_FONT['snap_to_grid'])
    # 首行缩进2字符 = 2 * 字号
    indent_pt = actual_size * BODY_FONT['first_line_indent_chars']
    p.paragraph_format.first_line_indent = Pt(indent_pt)
    if alignment == 'center':
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    elif alignment == 'right':
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT


# ============================================================
# 标题段落
# ============================================================
def add_heading(doc: Document, text: str, level: int):
    """添加标题段落

    宋体加粗，按层级设置字号（16/15/14/12pt）
    """
    p = doc.add_paragraph()
    run = p.add_run(text)
    size = HEADING_SIZES.get(level, HEADING_SIZES[4])
    _set_run_font(run,
                  cn_font=HEADING_FONT['cn'],
                  en_font=HEADING_FONT['en'],
                  size_pt=size,
                  bold=HEADING_FONT['bold'])
    _set_paragraph_spacing(p,
                           line_spacing=BODY_FONT['line_spacing'],
                           space_before=0,
                           space_after=0,
                           snap_to_grid=False)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT


# ============================================================
# 表格生成
# ============================================================
def _parse_html_table(html: str) -> List[List[str]]:
    """解析HTML表格，返回行列数据"""
    rows = []
    tr_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    td_pattern = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.DOTALL | re.IGNORECASE)

    for tr_match in tr_pattern.finditer(html):
        row = []
        for td_match in td_pattern.finditer(tr_match.group(1)):
            cell_text = td_match.group(1)
            # 去除嵌套HTML标签
            cell_text = re.sub(r'<[^>]+>', '', cell_text)
            # 解码HTML实体
            cell_text = cell_text.replace('&lt;', '<').replace('&gt;', '>')
            cell_text = cell_text.replace('&amp;', '&').replace('&nbsp;', ' ')
            cell_text = cell_text.strip()
            row.append(cell_text)
        if row:
            rows.append(row)
    return rows


def add_table(doc: Document, html: str, counter: FigureTableCounter):
    """添加表格

    - 表名在上方，居中，宋体小四号加粗
    - 表格居中，无文字环绕
    - 行高0.75cm
    - 表内文字宋体五号(10.5pt)，单倍行距
    - 重复标题行
    - 行不跨页拆分
    - 表名与表格同页
    """
    rows = _parse_html_table(html)
    if not rows:
        return

    num_cols = max(len(row) for row in rows)
    if num_cols == 0:
        return

    # 1. 添加表名（在表格上方）
    table_name = counter.next_table_number()
    name_paragraph = doc.add_paragraph()
    name_run = name_paragraph.add_run(table_name)
    _set_run_font(name_run,
                  cn_font=TABLE_CONFIG['name_font_cn'],
                  en_font=TABLE_CONFIG['name_font_en'],
                  size_pt=TABLE_CONFIG['name_font_size_pt'],
                  bold=TABLE_CONFIG['name_bold'])
    _set_paragraph_spacing(name_paragraph,
                           line_spacing=BODY_FONT['line_spacing'],
                           space_before=0,
                           space_after=0,
                           snap_to_grid=False)
    name_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    # 表名与表格保持在同一页
    name_paragraph.paragraph_format.keep_with_next = True

    # 2. 创建表格
    table = doc.add_table(rows=len(rows), cols=num_cols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    _set_table_borders(table)

    # 3. 填充表格内容并设置样式
    for i, row_data in enumerate(rows):
        row = table.rows[i]
        # 行高0.75cm
        _set_row_height(row, TABLE_CONFIG['row_height_cm'],
                        TABLE_CONFIG['row_height_rule'])
        # 行不跨页拆分
        if TABLE_CONFIG['cant_split_row']:
            _set_cant_split(row)
        # 第一行设为重复标题行
        if i == 0 and TABLE_CONFIG['repeat_header']:
            _set_repeat_header(row)

        for j in range(num_cols):
            cell_text = row_data[j] if j < len(row_data) else ''
            cell = row.cells[j]
            # 清除默认内容
            cell.text = ''
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run(cell_text)
            _set_run_font(run,
                          cn_font=TABLE_CONFIG['cell_font_cn'],
                          en_font=TABLE_CONFIG['cell_font_en'],
                          size_pt=TABLE_CONFIG['cell_font_size_pt'],
                          bold=False)
            _set_paragraph_spacing(paragraph,
                                   line_spacing=TABLE_CONFIG['cell_line_spacing'],
                                   space_before=TABLE_CONFIG['cell_space_before_pt'],
                                   space_after=TABLE_CONFIG['cell_space_after_pt'],
                                   snap_to_grid=False)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT


# ============================================================
# 图片插入
# ============================================================
def add_image(doc: Document, image_data: bytes, counter: FigureTableCounter):
    """添加图片

    - 嵌入型（inline），居中
    - 图名在下方，居中，分章编号
    - 图与名在同一页
    """
    if not image_data:
        return

    # 1. 插入图片（默认嵌入型）
    image_stream = io.BytesIO(image_data)
    try:
        doc.add_picture(image_stream, width=Cm(IMAGE_CONFIG['max_width_cm']))
    except Exception:
        return

    # 2. 设置图片段落居中，并与图名保持同页
    image_paragraph = doc.paragraphs[-1]
    image_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    image_paragraph.paragraph_format.keep_with_next = True

    # 3. 添加图名（在图下方，居中，分章编号）
    figure_name = counter.next_figure_number()
    name_paragraph = doc.add_paragraph()
    name_run = name_paragraph.add_run(figure_name)
    _set_run_font(name_run,
                  cn_font=IMAGE_CONFIG['name_font_cn'],
                  en_font=IMAGE_CONFIG['name_font_en'],
                  size_pt=IMAGE_CONFIG['name_font_size_pt'],
                  bold=IMAGE_CONFIG['name_bold'])
    _set_paragraph_spacing(name_paragraph,
                           line_spacing=BODY_FONT['line_spacing'],
                           space_before=0,
                           space_after=0,
                           snap_to_grid=False)
    name_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER


# ============================================================
# 主生成函数
# ============================================================
def generate_word(elements: List[ContentElement], output_path: str):
    """从结构化数据生成Word文档

    遍历所有ContentElement，按类型生成对应内容：
    - heading -> 标题段落
    - text -> 正文段落
    - table -> 表格（含表名）
    - image -> 图片（含图名）
    """
    doc = Document()

    # 1. 设置页面格式
    setup_page(doc)

    # 2. 设置默认Normal样式
    style = doc.styles['Normal']
    style.font.name = BODY_FONT['en']
    style.font.size = Pt(BODY_FONT['size_pt'])
    # 安全设置中文字体（w:eastAsia）
    rPr = style.element.get_or_add_rPr()
    rFonts = rPr.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        rPr.insert(0, rFonts)
    rFonts.set(qn('w:eastAsia'), BODY_FONT['cn'])

    # 3. 图表编号计数器
    counter = FigureTableCounter()

    # 4. 遍历内容元素
    for elem in elements:
        if elem.type == 'heading':
            # 更新章节号
            counter.update_chapter(elem.text, elem.heading_level or 1)
            # 添加标题
            add_heading(doc, elem.text, elem.heading_level or 1)

        elif elem.type == 'table':
            # 添加表格
            if elem.html:
                add_table(doc, elem.html, counter)

        elif elem.type == 'image':
            # 添加图片
            if elem.image_data:
                add_image(doc, elem.image_data, counter)

        else:
            # 正文
            text = elem.text.strip()
            if not text:
                continue

            # 过滤明显是页眉页脚的内容：单独成行且极短、纯数字
            if _is_likely_header_footer(text):
                continue

            add_body_paragraph(
                doc, text,
                size_pt=elem.mapped_size,
                alignment=elem.alignment or 'left',
                bold=bool(elem.is_bold),
            )

    # 5. 保存文档
    doc.save(output_path)
