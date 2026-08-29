# herdr HQ has no Python dependencies, so the image is the stdlib plus an ssh client.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 hq

WORKDIR /app
COPY --chown=hq:hq server.py collector.py attach.py config.example.json ./
COPY --chown=hq:hq static ./static
RUN chown hq:hq /app

USER hq
ENV PYTHONUNBUFFERED=1

# 127.0.0.1 is useless inside a container — bind the published port instead.
# Mount your config over /app/config.json and your ssh material read-only:
#   docker run -p 8787:8787 \
#     -v "$PWD/config.json:/app/config.json:ro" \
#     -v "$HOME/.ssh:/home/hq/.ssh:ro" \
#     ghcr.io/OWNER/herdr-hq
EXPOSE 8787
ENTRYPOINT ["python3", "server.py", "--host", "0.0.0.0", "--port", "8787"]
