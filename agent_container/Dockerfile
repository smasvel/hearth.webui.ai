FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=2.1.2
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-pip python3-venv default-jdk-headless maven build-essential pkg-config socat tar \
 && npm install --global "@opencode/cli@${OPENCODE_VERSION}" \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
EXPOSE 4096
ENTRYPOINT ["/bin/sh", "-lc", "umask 0002; socat TCP-LISTEN:4097,bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:/relay/model.sock & exec opencode serve --hostname 0.0.0.0 --port 4096"]
