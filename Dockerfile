FROM python:3.12-slim

# ============================================================
# 环境变量
# ============================================================
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # HuggingFace镜像（国内加速）
    HF_ENDPOINT=https://hf-mirror.com \
    # PaddleOCR模型存储路径
    PADDLE_OCR_HOME=/opt/paddleocr

# ============================================================
# 安装系统依赖（OpenCV等需要）
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
# 安装Python依赖
# ============================================================
COPY requirements.txt /app/requirements.txt
WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# ============================================================
# 预下载PaddleOCR模型（内置到镜像中，避免运行时下载）
# ============================================================
RUN mkdir -p /app/scripts
COPY scripts/predownload_models.py /app/scripts/predownload_models.py
RUN python scripts/predownload_models.py

# ============================================================
# 复制应用代码
# ============================================================
COPY . /app

# ============================================================
# 健康检查
# ============================================================
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=60s \
    CMD python -c "import urllib.request; r=urllib.request.urlopen('http://localhost:8000/api/health'); exit(0 if r.status==200 else 1)"

# ============================================================
# 启动
# ============================================================
EXPOSE 8000
CMD ["python", "run.py"]
