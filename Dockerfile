# herdr HQ. asyncssh replaces the OpenSSH client, so the image needs no ssh
# binary — but it does need your ssh material mounted (see below).
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 hq

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

USER hq
ENV PYTHONUNBUFFERED=1

# 127.0.0.1 is useless inside a container — bind the published port instead.
# Mount your config and ssh material read-only. asyncssh reads ~/.ssh/config,
# known_hosts and unencrypted private keys itself; for passphrase-protected
# keys forward the agent socket and set SSH_AUTH_SOCK instead.
#   docker run -p 8787:8787 \
#     -v "$PWD/herdr-hq.yaml:/home/hq/.config/herdr-hq/herdr-hq.yaml:ro" \
#     -v "$HOME/.ssh:/home/hq/.ssh:ro" \
#     ghcr.io/OWNER/herdr-hq
EXPOSE 8787
ENTRYPOINT ["herdr-hq", "--host", "0.0.0.0", "--port", "8787"]
