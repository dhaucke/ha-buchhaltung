ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HD_DATA_DIR=/data \
    HD_HOST=0.0.0.0 \
    HD_PORT=8099

RUN apk add --no-cache \
      bash \
      ca-certificates \
      libffi \
      libjpeg-turbo \
      openssl \
      zlib \
    && apk add --no-cache --virtual .build-deps \
      gcc \
      libffi-dev \
      musl-dev \
      openssl-dev \
      python3-dev \
      zlib-dev \
    && pip install --no-cache-dir \
      cryptography==45.0.7 \
      pillow==11.3.0 \
      pdfplumber==0.11.7 \
      reportlab==4.4.3 \
    && apk del .build-deps

COPY app /app
COPY run.sh /run.sh
RUN chmod +x /run.sh && mkdir -p /data/documents /data/archive

WORKDIR /app
EXPOSE 8099
CMD ["/run.sh"]
