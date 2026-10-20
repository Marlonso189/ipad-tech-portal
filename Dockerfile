FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ITP_HOST=0.0.0.0

LABEL org.opencontainers.image.source="https://github.com/Marlonso189/ipad-tech-portal" \
      org.opencontainers.image.title="ipad-tech-portal" \
      org.opencontainers.image.description="Internal portal for limited changes to Jamf Pro-managed iPads" \
      org.opencontainers.image.licenses="MIT"

RUN useradd --create-home --uid 10001 --user-group portal

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mock_jamf ./mock_jamf
COPY config.docker.example.yaml ./config.docker.example.yaml

RUN mkdir -p /app/logs && chown -R portal:portal /app

USER portal

EXPOSE 8741

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8741/api/health', timeout=3)"

CMD ["python", "-m", "app"]
