FROM python:3.12-slim-bookworm AS dependencies
ENV PYTHONUNBUFFERED=1 \
    INFIAGENT_USER_ROOT=/data/user_root \
    MLA_USER_DATA_ROOT=/data/user_root \
    TZ=Asia/Shanghai \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini ca-certificates curl tzdata libatomic1 pandoc fonts-noto-cjk tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
COPY docker/constraints-linux-py312.txt /tmp/constraints.txt
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install -c /tmp/constraints.txt -r /tmp/requirements.txt
RUN python -m playwright install --with-deps chromium

FROM dependencies AS runtime
ARG VERSION=3.13.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="InfiAgent Runtime" \
      org.opencontainers.image.source="https://github.com/polyuiislab/infiAgent" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
COPY dist/infiagent-3.13.0-py3-none-any.whl /tmp/infiagent.whl
RUN mv /tmp/infiagent.whl /tmp/infiagent-3.13.0-py3-none-any.whl \
    && python -m pip install --no-deps /tmp/infiagent-3.13.0-py3-none-any.whl \
    && python -m pip check \
    && rm -f /tmp/infiagent-3.13.0-py3-none-any.whl
COPY docker/entrypoint.sh /usr/local/bin/infiagent-entrypoint
RUN chmod +x /usr/local/bin/infiagent-entrypoint && mkdir -p /data/user_root
WORKDIR /data/user_root
EXPOSE 4260
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD curl -fsS http://127.0.0.1:4260/health || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/infiagent-entrypoint"]
CMD ["runtime"]
