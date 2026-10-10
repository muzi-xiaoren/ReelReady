FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    REELREADY_DATA_DIR=/data \
    REELREADY_PORT=8765 \
    TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY reelready ./reelready
# Vendor htmx so the UI works without reaching a CDN.
ADD --chmod=644 https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js ./reelready/web/static/vendor/htmx.min.js

# Set by the release workflow from the git tag.
ARG REELREADY_VERSION=dev
ENV REELREADY_VERSION=${REELREADY_VERSION}

VOLUME ["/data"]
EXPOSE 8765

CMD ["python", "-m", "reelready"]
