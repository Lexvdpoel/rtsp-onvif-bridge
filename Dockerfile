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

# VA-API drivers, so hardware encoding can use an Intel or AMD render node.
# Optional: the image stays fully usable with software encoding if the mirror
# does not carry them, so a failure here must not fail the build.
RUN apt-get update && \
    (apt-get install -y --no-install-recommends \
        libva2 libva-drm2 vainfo intel-media-va-driver mesa-va-drivers \
     || echo "VA-API drivers unavailable; hardware encoding will not work") \
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

# COCO-trained YOLOX from Megvii (Apache-2.0), baked in so the cameras never
# fetch anything at runtime. Two sizes, because the cost is per camera and not
# every host can afford the larger one:
#
#   tiny  416px   ~130 ms a frame on two threads
#   s     640px   ~475 ms a frame on two threads, and better on small or
#                 distant things, which is the case that matters outdoors
#
# Apache-2.0 matters here. The better-known YOLOv8 is AGPL-3.0, which would
# make this project's own licence unusable.
ARG YOLOX_RELEASE=https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0
RUN mkdir -p /opt/models     && curl -fsSL -o /opt/models/yolox_tiny.onnx "${YOLOX_RELEASE}/yolox_tiny.onnx"     && curl -fsSL -o /opt/models/yolox_s.onnx "${YOLOX_RELEASE}/yolox_s.onnx"

# ONNX Runtime on an NVIDIA card, for anyone who has one. Off by default: the
# GPU build pulls in the CUDA libraries and adds a couple of gigabytes to an
# image that is otherwise a few hundred megabytes, which is a poor trade for a
# host without a card.
#
#   docker build --build-arg DETECT_GPU=1 -t rtsp-onvif-bridge:latest .
#
# It also needs the NVIDIA container runtime on the host; the controller only
# asks for a GPU when the image was built this way, so a host without one is
# not left with containers that refuse to start.
ARG DETECT_GPU=0
RUN if [ "${DETECT_GPU}" = "1" ]; then         pip install --no-cache-dir --force-reinstall onnxruntime-gpu==1.20.1         && python -c "import onnxruntime; print('providers:', onnxruntime.get_available_providers())";     else         echo "CPU build; rebuild with --build-arg DETECT_GPU=1 for an NVIDIA card";     fi
ENV DETECT_GPU_BUILD=${DETECT_GPU}

WORKDIR /opt/bridge

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/udhcpc.script /usr/local/share/udhcpc.script
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/share/udhcpc.script /usr/local/bin/entrypoint.sh

COPY app ./app

# The same blue icon the Unraid tile uses, so the browser tab matches it.
# Copied in beside the controller rather than kept in two places.
COPY unraid/icon.png ./app/controller/icon.png

ENV ROLE=controller \
    STATE_DIR=/state \
    DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
