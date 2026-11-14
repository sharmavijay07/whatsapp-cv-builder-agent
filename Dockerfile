# syntax=docker/dockerfile:1
# Free Render web service with Tesseract, for a NEW service.
# The existing whatsapp-cv-builder service is runtime python, which Render
# cannot change in place. Photo reading on that service uses Groq vision.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/tmp/safu \
    PORT=10000

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd --create-home --uid 1001 safu \
    && mkdir -p /tmp/safu \
    && chown -R safu:safu /app /tmp/safu
USER safu

EXPOSE 10000
CMD ["python", "-m", "safu"]
