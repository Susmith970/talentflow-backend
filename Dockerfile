FROM python:3.12-slim

# System deps for Playwright/Chromium on Debian bookworm (python:3.12-slim)
# Note: libasound2t64 is the correct package name in Debian bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2t64 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 libwayland-client0 \
    fonts-liberation xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium at BUILD time (not startup — avoids health check timeout)
RUN playwright install chromium --with-deps

COPY . .
RUN mkdir -p data/resumes

EXPOSE 8080

CMD exec gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
