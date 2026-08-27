FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

# 只安装 Python 客户端；基础镜像已经包含版本一致的 Chromium。
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 先复制稳定依赖，再复制业务代码，便于 Docker 使用构建缓存。
COPY core.py customer_notes.py bot.py /app/
WORKDIR /app

# 日志立即输出，方便评测器和人工 review 定位问题。
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
