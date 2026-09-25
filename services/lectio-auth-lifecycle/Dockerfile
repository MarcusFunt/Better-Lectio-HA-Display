FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

EXPOSE 8766
CMD ["python", "-m", "uvicorn", "lectio_auth_lifecycle.main:app", "--host", "0.0.0.0", "--port", "8766"]
