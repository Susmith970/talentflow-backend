FROM python:3.12-slim

# Install system dependencies needed by Playwright/Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 libwayland-client0 \
    fonts-liberation libappindicator3-1 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium
RUN playwright install chromium --with-deps

# Copy all source files
COPY . .

# Create data directory
RUN mkdir -p data/resumes

EXPOSE 8000

CMD gunicorn app:app \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 2 \
    --timeout 300 \
    --preload \
    --access-logfile - \
    --error-logfile -
