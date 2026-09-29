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
        netcat-openbsd \
        ca-certificates \
        curl \
        openssl \
        python3-venv \
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

# COCO-trained SSD MobileNet v1 from the ONNX model zoo (Apache-2.0). Baked in
# so the cameras never fetch it at runtime. It does its own non-maximum
# suppression, so what comes out is already a short list of boxes.
ARG MODEL_URL=https://github.com/onnx/models/raw/main/validated/vision/object_detection_segmentation/ssd-mobilenetv1/model/ssd_mobilenet_v1_10.onnx
RUN mkdir -p /opt/models &&     curl -fsSL -o /opt/models/ssd_mobilenet_v1_10.onnx "${MODEL_URL}"

WORKDIR /opt/bridge

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# unifi-cam-proxy, for the optional UniFi Protect mode, in a virtualenv of its
# own. Three reasons it cannot share the environment above: its dependencies are
# unpinned and pyunifiprotect would drag in a pydantic that fights with
# FastAPI's; it declares support up to Python 3.11 while this image runs 3.12,
# so the venv is built on Debian's python3; and it is not on PyPI in any current
# form, so it comes from a pinned commit.
#
# One of its pinned requirements, pyunifiprotect, was renamed to uiprotect and
# removed from PyPI, so the requirements no longer install as published. The
# successor is swapped in and the old import path given back with a shim:
# unifi/main.py is the only place that imports it, and only for the top-level
# ProtectApiClient. Pillow and OpenCV are added for a related reason: a backend
# this project never uses pulls them in, and while the failed import is not fatal
# it prints an ImportError on every start that reads like a fault.
#
# The install is allowed to fail. It is an opt-in feature, the upstream project
# pins nothing and pulls one dependency straight from a branch archive, so a
# break there should not cost everyone else their image. A camera set to UniFi
# mode without it says so plainly instead of failing quietly. The import is
# checked here so a half-working install fails at build time, not at adoption.
ARG UNIFI_CAM_PROXY_REF=cc6d3fc7cdae9f1dfce575627089632aec696403
RUN set -eu; \
    python3 -m venv /opt/unifi-venv; \
    if curl -fsSL -o /tmp/ucp-requirements.txt \
        "https://raw.githubusercontent.com/keshavdv/unifi-cam-proxy/${UNIFI_CAM_PROXY_REF}/requirements.txt"; \
    then \
        sed -i '/^pyunifiprotect/d' /tmp/ucp-requirements.txt; \
        echo "uiprotect" >> /tmp/ucp-requirements.txt; \
        echo "pillow" >> /tmp/ucp-requirements.txt; \
        echo "opencv-python-headless" >> /tmp/ucp-requirements.txt; \
    fi; \
    if /opt/unifi-venv/bin/pip install --no-cache-dir -r /tmp/ucp-requirements.txt \
       && /opt/unifi-venv/bin/pip install --no-cache-dir --no-deps \
        "https://github.com/keshavdv/unifi-cam-proxy/archive/${UNIFI_CAM_PROXY_REF}.tar.gz"; \
    then \
        printf '%s\n' \
            '"""Compatibility shim.' \
            '' \
            'unifi-cam-proxy imports pyunifiprotect, which was renamed to uiprotect' \
            'and removed from PyPI. Only ProtectApiClient is used, from the top level.' \
            '"""' \
            'from uiprotect import *  # noqa: F401,F403' \
            'from uiprotect import ProtectApiClient  # noqa: F401' \
            > "$(/opt/unifi-venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/pyunifiprotect.py"; \
        /opt/unifi-venv/bin/python -c 'import unifi.main; print("unifi-cam-proxy imports cleanly")'; \
        echo "unifi-cam-proxy installed at ${UNIFI_CAM_PROXY_REF}"; \
    else \
        echo "WARNING: unifi-cam-proxy could not be installed; UniFi mode will be unavailable"; \
        rm -rf /opt/unifi-venv; \
    fi; \
    rm -f /tmp/ucp-requirements.txt

COPY docker/udhcpc.script /usr/local/share/udhcpc.script
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/share/udhcpc.script /usr/local/bin/entrypoint.sh

COPY app ./app

ENV ROLE=controller \
    STATE_DIR=/state \
    DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
