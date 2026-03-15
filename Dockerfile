FROM registry.cn-hangzhou.aliyuncs.com/library/python:3.11-slim

WORKDIR /app

# 安装依赖（使用清华镜像加速，避免国内网络超时）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制源码（不复制 config.yaml，web 模式不需要）
COPY app.py main.py ./
COPY templates/ templates/

# 数据目录由 volume 挂载
RUN mkdir -p /data/logs

ENV PORT=8080 \
    WXREAD_DB=/data/wxread.db \
    WXREAD_LOG=/data/logs

EXPOSE 8080

CMD ["python", "app.py"]
