FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY deploy/sql /app/deploy/sql
COPY backend /app/backend
RUN pip install --no-cache-dir --no-deps /app/backend
USER 65534:65534
EXPOSE 8000
CMD ["uvicorn", "agent_runtime.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Optional deployment target for an independently packaged customer extension.
FROM base AS with-extension
USER root
ARG EXTENSION_DIR
COPY ${EXTENSION_DIR}/ /tmp/runtime-extension/
RUN pip install --no-cache-dir --no-deps /tmp/runtime-extension && rm -rf /tmp/runtime-extension
USER 65534:65534

FROM base AS runtime
