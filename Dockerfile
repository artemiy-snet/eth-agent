# Каркас образу (Біблія, розділ 12). Веб-панель і live-контур додаються на етапах 9–10.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
RUN pip install --no-cache-dir .
VOLUME ["/data"]
ENTRYPOINT ["eth-agent", "--data", "/data"]
CMD ["--help"]
