# -*- coding: utf-8 -*-
"""启动入口

支持通过环境变量配置：
  HOST - 监听地址（默认 0.0.0.0）
  PORT - 监听端口（默认 8000）
  RELOAD - 是否开启热重载（默认 false，开发时设为 true）
"""

import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    reload = os.environ.get("RELOAD", "false").lower() == "true"

    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=reload,
    )
