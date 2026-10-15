FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY app_server.py parser.py server.py smyvy.html refresh_embedded_history.py ./

ENV SMYVY_HOST=0.0.0.0 \
    SMYVY_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf "http://127.0.0.1:8000/input" || exit 1

CMD ["python", "-u", "app_server.py"]
