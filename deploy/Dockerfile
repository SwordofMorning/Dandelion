FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY mk ./mk
RUN pip install --no-cache-dir . \
    && mkdir -p /data /workspace \
    && chown -R 1000:1000 /data /workspace

USER 1000:1000
EXPOSE 8765
CMD ["python", "-m", "src.remote", "serve", "--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data", "--workspace", "/workspace", "--config", "/run/secrets/api.cfg"]
