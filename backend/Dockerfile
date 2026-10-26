# Tesseract (OCR for scanned CVs) and LibreOffice (Word -> PDF) cannot be installed on
# Render's plain Python runtime, so the API is deployed as a Docker service.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 \
        libreoffice-writer fonts-liberation fonts-dejavu-core fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
