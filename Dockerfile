FROM python:3.13-alpine

# Supplied by the build tool (CLAUDE_MONITOR_VERSION build argument).
ARG CLAUDE_MONITOR_VERSION=dev

LABEL org.opencontainers.image.title="Claude Monitor" \
      org.opencontainers.image.description="Claude Code quota bridge for Homepage" \
      org.opencontainers.image.version="${CLAUDE_MONITOR_VERSION}"

WORKDIR /app
COPY app.py /app/app.py

RUN mkdir -p /data

ENV PORT=8787 DATA_FILE=/data/quota.json
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
      CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8787/healthz', timeout=3).close()"

CMD ["python", "/app/app.py"]
