FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml ./
# git fetches py-identity-model, pinned to a commit (pyproject.toml); it is
# removed again in the same layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && pip install --no-cache-dir . \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
