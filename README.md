# PDF转Word格式规范化系统

将 PDF 文档自动转换为符合固定格式规范的 Word 文档，保留原始内容的标题层级、表格、图片等结构，并统一应用标准排版格式。

## 功能特性

- **双路径提取**：自动检测 PDF 类型
  - 原生 PDF：使用 PyMuPDF 直接提取精确的字体名、字号、粗体/斜体信息
  - 扫描件：使用 PaddleOCR PP-StructureV3 进行版面分析 + OCR 识别
- **格式规范化**：按固定模板生成 Word 文档
  - 页面：A4，上下边距 2.54cm，左右 3.18cm，页眉 1.5cm，页脚 1.75cm
  - 正文：宋体 + Times New Roman，小四号 (12pt)，1.5 倍行距，首行缩进 2 字符
  - 标题：宋体加粗，一级三号 / 二级小三号 / 三级四号 / 四级及以下小四号
  - 表格：居中，行高 0.75cm，表内五号字，表名小四加粗居中于上方，重复标题行，不跨页
  - 图片：居中嵌入型，图名居中于下方，分章编号，不跨页
- **智能标题推断**：多信号融合（文本编号模式 + 字号 + 加粗）识别标题层级
- **Web 界面**：FastAPI 驱动，浏览器上传 PDF 即可下载 Word

## 快速开始

### Docker 部署（推荐）

```bash
# 克隆仓库
git clone https://github.com/SmallStom/pdf2word.git
cd pdf2word

# 构建并启动
docker-compose up -d

# 访问 http://localhost:8000
```

首次启动会自动从百度云下载 PaddleX 模型（约 1-2GB，需 5-15 分钟），后续启动秒级就绪。

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 如需支持扫描件，还需安装 PaddleOCR（体积较大）
pip install paddlepaddle paddleocr

# 启动服务
python run.py

# 访问 http://localhost:8000
```

## 使用方法

1. 浏览器打开 `http://localhost:8000`
2. 点击或拖拽上传 PDF 文件
3. 等待转换完成
4. 下载生成的 Word 文档

## 项目结构

```
pdf2word/
├── app.py                      # FastAPI Web 应用（路由 + 文件上传/下载）
├── config.py                   # 格式规范配置（页边距、字号映射、标题模式等）
├── pdf_extractor.py            # PDF 内容提取（原生 PDF + 扫描件双路径）
├── style_mapper.py             # 样式映射 & 标题层级推断 & 图表分章编号
├── word_generator.py           # Word 文档生成（python-docx，全部格式规范）
├── pipeline.py                 # 主流程编排（检测 -> 提取 -> 映射 -> 生成）
├── run.py                      # 启动入口（支持环境变量配置）
├── requirements.txt            # Python 依赖
├── static/
│   └── index.html              # Web 上传/下载页面
├── scripts/
│   └── predownload_models.py   # PaddleX 模型预下载脚本（build 阶段）
├── Dockerfile                  # Docker 镜像构建
├── docker-compose.yml          # Docker Compose 编排
├── pip.conf                    # pip 清华镜像源配置
└── .dockerignore
```

## 技术栈

| 模块 | 技术选型 | 说明 |
|:---|:---|:---|
| PDF 文本/字体提取 | PyMuPDF (fitz) | 原生 PDF 直接读取字体元数据（字体名、字号、粗体/斜体） |
| PDF 表格提取 | pdfplumber | 基于 PDF 线条检测提取表格结构 |
| 扫描件 OCR | PaddleOCR PP-StructureV3 | 版面分析 + 文字识别 + 表格识别 |
| Word 生成 | python-docx | 直接构建文档，精确控制格式 |
| Web 框架 | FastAPI + Uvicorn | 异步 Web 服务 |
| 部署 | Docker + Docker Compose | 容器化部署，国内镜像源加速 |

## 格式规范详解

### 页面设置

| 项目 | 值 |
|:---|:---|
| 纸型 | A4 (21cm × 29.7cm) |
| 上边距 | 2.54cm |
| 下边距 | 2.54cm |
| 左边距 | 3.18cm |
| 右边距 | 3.18cm |
| 页眉距边界 | 1.5cm |
| 页脚距边界 | 1.75cm |

### 正文

- 中文字体：宋体
- 英文字体：Times New Roman
- 字号：小四号 (12pt)
- 行距：1.5 倍行距
- 段前段后：0
- 取消网格对齐
- 首行缩进：2 字符

### 标题

| 层级 | 字体 | 字号 | 磅值 |
|:---|:---|:---|:---|
| 一级 | 宋体加粗 | 三号 | 16pt |
| 二级 | 宋体加粗 | 小三号 | 15pt |
| 三级 | 宋体加粗 | 四号 | 14pt |
| 四级及以上 | 宋体加粗 | 小四号 | 12pt |

### 表格

- 居中排列，无文字环绕
- 行高 0.7–0.8cm（取 0.75cm，内容多时自动扩展）
- 表内文字：宋体，五号 (10.5pt)，单倍行距
- 表名：位于表上方，居中，宋体，小四号 (12pt)，加粗
- 跨页表格自动重复标题行
- 表格行不跨页拆分，表名与表格保持在同一页

### 图片

- 居中排列，嵌入型 (inline)
- 图名位于图下方，居中，分章编号（如"图 1-1"）
- 图片及名称保持在同一页

## Docker 部署说明

### 国内加速

Docker 构建已配置以下国内镜像源加速：

| 加速项 | 镜像源 |
|:---|:---|
| pip 包 | 清华大学 `pypi.tuna.tsinghua.edu.cn` |
| PaddleOCR 模型 | 百度云 `paddleocr.bj.bcebos.com`（国内直连） |
| HuggingFace | 镜像站 `hf-mirror.com` |

### 构建命令

```bash
# 方式一：Docker Compose（推荐）
docker-compose up -d

# 方式二：手动构建
docker build -t pdf2word .
docker run -d -p 8000:8000 --name pdf2word pdf2word
```

### 模型缓存（命名卷）

PaddleX 模型默认缓存在 `~/.paddlex/official_models/`，**docker-compose.yml 已挂载为命名卷 `pdf2word_models`**，避免每次重建镜像都重新下载（约 1-2GB）。

- **首次启动**：容器内检测到卷是空的，会自动从百度云下载模型到卷中（约 5-15 分钟）
- **后续启动**：直接复用卷中的模型，秒级就绪
- **重建镜像**：`docker-compose build --no-cache` 不会影响模型卷，模型不丢

```bash
# 查看模型卷大小
docker volume inspect pdf2word_pdf2word_models

# 备份/迁移模型卷到其他机器
docker run --rm -v pdf2word_pdf2word_models:/from -v $(pwd):/to \
    alpine tar czf /to/pdf2word_models.tar.gz -C /from .

# 删除模型卷（下次启动会重新下载）
docker volume rm pdf2word_pdf2word_models
```

### 环境变量

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `RELOAD` | `false` | 是否开启热重载（开发模式） |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HuggingFace 镜像地址 |
| `PADDLE_OCR_HOME` | `/opt/paddleocr` | PaddleOCR 模型存储路径 |

## API 接口

### `GET /`

返回 Web 上传页面。

### `POST /api/convert`

上传 PDF 文件并转换为 Word。

**请求**：`multipart/form-data`，字段 `file` 为 PDF 文件。

**响应**：直接返回 `.docx` 文件下载。

```bash
curl -X POST http://localhost:8000/api/convert \
  -F "file=@document.pdf" \
  -o output.docx
```

### `GET /api/health`

健康检查接口。

```json
{"status": "ok", "service": "PDF转Word格式规范化系统"}
```

## 标题识别规则

系统通过多信号融合推断标题层级：

1. **文本编号模式**（最优先）
   - `第X章/部分/编/篇` → 一级标题
   - `第X节/条/款` → 二级标题
   - `X.X` → 二级标题
   - `X.X.X` → 三级标题
   - `（X）` / `(X)` → 三级/四级标题

2. **字号判断**（原生 PDF）
   - ≥16pt → 一级
   - ≥15pt → 二级
   - ≥14pt → 三级

3. **加粗 + 字号比例**（辅助信号）
   - 字号明显大于正文且加粗 → 对应层级标题

## 许可证

MIT License
