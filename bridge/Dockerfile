FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ATVB_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY atvbridge ./atvbridge

VOLUME /data
# 8090: dashboard + companion app WebSocket, 6466: remote, 6467: pairing.
# Run with host networking so phones can discover the bridge over mDNS.
EXPOSE 8090 6466 6467

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/state' % os.environ.get('ATVB_WEB_PORT', '8090'), timeout=4)"

CMD ["python", "-m", "atvbridge"]
