FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBD_CONFIG_DIR=/app/config \
    JOBD_DB_URL=sqlite:////app/data/jobd.db \
    JOBD_LOGS_DIR=/app/logs \
    JOBD_PORT=8765

COPY pyproject.toml ./
COPY src ./src
RUN pip install -U pip && pip install .

RUN mkdir -p /app/data /app/logs
EXPOSE 8765

CMD ["jobd"]
