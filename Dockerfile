# ============================================================
# PDF转Word - GPU版Dockerfile（默认）
# ============================================================
# 需要 NVIDIA Docker runtime 和 NVIDIA 驱动
# 显存限制通过环境变量 PADDLE_GPU_MEMORY_GB 控制（默认4GB）
# ============================================================

FROM ccr-2vdh3abv-pub.cnc.bj.baidubce.com/paddlepaddle/paddle:3.0.0-gpu-cuda11.8-cudnn8.9-trt8.6

# ============================================================
# 环境变量
# ============================================================
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    PADDLE_OCR_HOME=/opt/paddleocr \
    PADDLE_DEVICE=gpu \
    # GPU显存限制（GB），显卡共享时设置此项
    PADDLE_GPU_MEMORY_GB=4

# ============================================================
# 安装系统依赖
# ============================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ============================================================
# 配置pip国内镜像源（清华源）
# ============================================================
COPY pip.conf /etc/pip.conf

# ============================================================
# 安装Python依赖（基础镜像已含paddlepaddle-gpu，此处安装其余依赖）
# 基础镜像预装了PyYAML等老版本distutils包，使用 --ignore-installed 强制覆盖
# ============================================================
COPY requirements.txt /app/requirements.txt
WORKDIR /app
RUN pip install --no-cache-dir --ignore-installed \
    PyMuPDF pdfplumber paddleocr "paddlex[ocr]" python-docx \
    fastapi uvicorn python-multipart \
    opencv-python-headless numpy Pillow

# ============================================================
# 预下载PaddleX模型（写到镜像层 ~/.paddlex/official_models/）
# 注意：运行时若挂载了 pdf2word_models 卷到 /root/.paddlex，此处下载的
# 模型会被卷覆盖（首次启动会重新下载到卷中）。此步骤主要是为了
# 让"无卷"场景（如开发调试）也能直接用。
# ============================================================
RUN mkdir -p /app/scripts
COPY scripts/predownload_models.py /app/scripts/predownload_models.py
RUN python scripts/predownload_models.py || echo "模型预下载失败（不影响构建）"

# ============================================================
# 复制应用代码
# ============================================================
COPY . /app

# ============================================================
# 健康检查
# ============================================================
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/api/health'); exit(0 if r.status==200 else 1)"

EXPOSE 8000
CMD ["python", "run.py"]
