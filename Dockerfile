# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ARG TARGETARCH=amd64
ARG MEDIAMTX_VERSION=1.9.3

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        busybox \
        iproute2 \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# MediaMTX is used as the per-camera RTSP relay so the stream appears to
# originate from the virtual camera's own IP address.
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64)  MTX_ARCH=amd64 ;; \
        arm64)  MTX_ARCH=arm64v8 ;; \
        arm)    MTX_ARCH=armv7 ;; \
        *)      MTX_ARCH=amd64 ;; \
    esac; \
    curl -fsSL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${MTX_ARCH}.tar.gz"; \
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin mediamtx; \
    rm -f /tmp/mediamtx.tar.gz; \
    chmod +x /usr/local/bin/mediamtx

WORKDIR /opt/bridge

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/udhcpc.script /usr/local/share/udhcpc.script
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/share/udhcpc.script /usr/local/bin/entrypoint.sh

COPY app ./app

ENV ROLE=controller \
    STATE_DIR=/state \
    DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
