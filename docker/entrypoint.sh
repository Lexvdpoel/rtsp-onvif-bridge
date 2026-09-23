#!/bin/sh
set -e

case "${ROLE:-controller}" in
    controller)
        exec uvicorn app.controller.main:app \
            --host 0.0.0.0 \
            --port "${WEB_PORT:-8080}" \
            --log-level "${LOG_LEVEL:-info}"
        ;;
    camera)
        exec python -m app.camera.run
        ;;
    *)
        echo "Unknown ROLE '${ROLE}'. Use 'controller' or 'camera'." >&2
        exit 1
        ;;
esac
