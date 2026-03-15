FROM python:3.11-slim

WORKDIR /app

# 安装依赖（单独一层，利用缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY app.py main.py config.yaml ./
COPY templates/ templates/

# 数据目录（数据库 + 日志）由 volume 挂载
RUN mkdir -p /data/logs

ENV PORT=8080 \
    WXREAD_DB=/data/wxread.db \
    WXREAD_LOG=/data/logs

EXPOSE 8080

CMD ["python", "app.py"]
