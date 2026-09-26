FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home service \
    && mkdir -p /var/lib/better-lectio /var/lib/better-lectio-ha-setup /var/lib/better-lectio-api-auth \
    && chown -R service:service /var/lib/better-lectio /var/lib/better-lectio-ha-setup /var/lib/better-lectio-api-auth

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "lectio_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
