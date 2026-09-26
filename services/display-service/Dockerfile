FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home service \
    && mkdir -p /var/lib/better-lectio-display /var/lib/better-lectio-ha-setup \
    && chown -R service:service /var/lib/better-lectio-display /var/lib/better-lectio-ha-setup

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "display_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
