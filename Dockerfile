FROM python:3.12-slim

WORKDIR /app

# Dependances systeme (Pillow a besoin de libjpeg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    zlib1g \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

# 1 worker car on stocke les jobs en RAM (sync entre threads, pas entre processes)
# Timeout 0 = pas de timeout pour les long imports
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
