FROM docker:29-cli AS docker_cli
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/hearth
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git iproute2 libatomic1 && rm -rf /var/lib/apt/lists/*
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium
COPY . .
CMD ["python", "deploy/docker-entrypoint.py"]
