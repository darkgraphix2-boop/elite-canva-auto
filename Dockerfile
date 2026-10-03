FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ELITE_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && playwright install --with-deps chromium && patchright install chromium && apt-get update && apt-get install -y --no-install-recommends xvfb xauth && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & sleep 2; export DISPLAY=:99; exec python -u deploy_app.py"]
