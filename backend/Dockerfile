FROM python:3.12-slim
WORKDIR /app

# Dependencies come from uv.lock, the versions the tests ran against, never a
# fresh resolve of pyproject.toml's ranges. They go into the image's own Python
# (UV_PROJECT_ENVIRONMENT), without the dev group; the app runs from /app.
ENV UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.5 \
    && uv sync --locked --no-dev --no-install-project \
    && pip uninstall -y uv

COPY . .

RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
