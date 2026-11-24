# papertrader - simulation only, needs no credentials.
FROM python:3.14-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY papertrader ./papertrader
RUN pip install --no-cache-dir .
# state/, logs/, cache/ and config.yaml are mounted as volumes (see docker-compose.yml)
CMD ["python", "-m", "papertrader", "run", "--config", "/app/config.yaml"]
