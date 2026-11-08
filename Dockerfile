# FL.ru Parser Dockerfile
# Образ с планировщиком supercronic: расписание задаётся переменными CRON_PARSE и CRON_STATS

FROM python:3.11-slim

# Метаданные
LABEL maintainer="FL.ru Parser"
LABEL description="FL.ru RSS parser with AI analysis for DevOps tasks"
LABEL org.opencontainers.image.source="https://github.com/ru-admin/flparser"

# Рабочая директория
WORKDIR /app

# supercronic — планировщик cron-задач (версия и SHA256 из релиза aptible/supercronic)
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.51
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) SUPERCRONIC_SHA256=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
        arm64) SUPERCRONIC_SHA256=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*

# Копируем исходный код и entrypoint
COPY flparser.py .
COPY flparser .
COPY docker-entrypoint.sh .

# Делаем скрипты исполняемыми
RUN chmod +x flparser docker-entrypoint.sh

# Каталог данных (БД и логи) и непривилегированный пользователь (UID 1000)
RUN mkdir -p /app/data && \
    useradd -m -u 1000 -s /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

# Entrypoint: стартовая проверка (--stats без отправки в Telegram), затем supercronic
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Healthcheck (проверяет, что контейнер жив)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1
