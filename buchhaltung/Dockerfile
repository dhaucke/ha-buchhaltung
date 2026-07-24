ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HD_DATA_DIR=/data \
    HD_HOST=0.0.0.0 \
    HD_PORT=8099 \
    MUSTANG_CLI_JAR=/opt/mustang/Mustang-CLI.jar

ARG MUSTANG_CLI_VERSION=2.24.0

RUN apk add --no-cache \
      bash \
      ca-certificates \
      libffi \
      libjpeg-turbo \
      openjdk21-jre-headless \
      openssl \
      zlib \
    && apk add --no-cache --virtual .build-deps \
      curl \
      gcc \
      libffi-dev \
      musl-dev \
      openssl-dev \
      python3-dev \
      zlib-dev \
    && pip install --no-cache-dir \
      cryptography==45.0.7 \
      factur-x==6.6 \
      pillow==11.3.0 \
      pdfplumber==0.11.7 \
      reportlab==4.4.3 \
    && mkdir -p /opt/mustang \
    && curl -fsSL -o "${MUSTANG_CLI_JAR}" \
      "https://github.com/ZUGFeRD/mustangproject/releases/download/core-${MUSTANG_CLI_VERSION}/Mustang-CLI-${MUSTANG_CLI_VERSION}.jar" \
    && apk del .build-deps

COPY app /app
COPY run.sh /run.sh
RUN chmod +x /run.sh && mkdir -p /data/documents /data/archive /data/reports

WORKDIR /app
EXPOSE 8099
CMD ["/run.sh"]
