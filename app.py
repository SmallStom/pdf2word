# -*- coding: utf-8 -*-
"""FastAPI Web应用 - PDF转Word转换服务"""

import os
import uuid
import tempfile
import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pipeline import PDFToWordPipeline

# 日志配置
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 路径配置
BASE_DIR = Path(__file__).parent.resolve()
STATIC_DIR = BASE_DIR / "static"
TEMP_DIR = Path(tempfile.gettempdir()) / "pdf2word"
TEMP_DIR.mkdir(exist_ok=True)

# FastAPI应用
app = FastAPI(title="PDF转Word格式规范化系统", version="1.0.0")

# 静态文件服务
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回上传页面"""
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.post("/api/convert")
async def convert_pdf(file: UploadFile = File(...), background_tasks: BackgroundTasks = None):
    """上传PDF并转换为Word

    接收PDF文件，自动检测类型（原生/扫描），提取内容并按格式规范生成Word文档。
    """
    # 验证文件类型
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="请上传PDF格式文件")

    # 保存上传的PDF到临时文件
    pdf_id = str(uuid.uuid4())
    pdf_path = str(TEMP_DIR / f"{pdf_id}.pdf")
    docx_path = str(TEMP_DIR / f"{pdf_id}.docx")

    try:
        content = await file.read()
        with open(pdf_path, 'wb') as f:
            f.write(content)
        logger.info(f"已保存上传文件: {file.filename} ({len(content)} bytes)")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件保存失败: {str(e)}")

    # 执行转换
    try:
        pipeline = PDFToWordPipeline()
        pipeline.process(pdf_path, docx_path)
    except ImportError as e:
        _cleanup_file(pdf_path)
        _cleanup_file(docx_path)
        raise HTTPException(
            status_code=503,
            detail=f"缺少必要依赖: {str(e)}。扫描件转换需要安装PaddleOCR。"
        )
    except Exception as e:
        logger.exception("转换过程中发生错误")
        _cleanup_file(pdf_path)
        _cleanup_file(docx_path)
        raise HTTPException(status_code=500, detail=f"转换失败: {str(e)}")

    # 生成输出文件名
    output_filename = file.filename.rsplit('.', 1)[0] + '.docx'

    # 注册清理任务（响应发送后执行）
    if background_tasks:
        background_tasks.add_task(_cleanup_file, pdf_path)
        background_tasks.add_task(_cleanup_file, docx_path)

    return FileResponse(
        path=docx_path,
        filename=output_filename,
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


@app.get("/api/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "PDF转Word格式规范化系统"}


def _cleanup_file(file_path: str):
    """清理临时文件"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass
