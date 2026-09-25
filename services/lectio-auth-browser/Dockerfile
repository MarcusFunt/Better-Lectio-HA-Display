FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY entrypoint.sh /usr/local/bin/lectio-auth-browser-entrypoint

RUN chmod 0755 /usr/local/bin/lectio-auth-browser-entrypoint \
    && python -m pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc openbox novnc websockify fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10002 --create-home service \
    && chown -R service:service /app /home/service /ms-playwright

USER 10002:10002
EXPOSE 8765 6080
CMD ["/usr/local/bin/lectio-auth-browser-entrypoint"]
