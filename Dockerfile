FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

VOLUME ["/data", "/music"]
ENV DATA_DIR=/data \
    MUSIC_DIR=/music

EXPOSE 8080
# umask 000: 库文件保持 666/777, 避免"容器写文件→宿主读不了"的权限事故
CMD ["sh", "-c", "umask 000 && exec python -m app serve"]
