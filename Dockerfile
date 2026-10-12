# Static binaries of the Docker client and the compose plugin (to recreate the Icarus container).
FROM docker:29-cli AS dockercli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONFIG=/tmp/.docker

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY warden ./warden

# Never root: uid 1000 (owner of the Icarus files); access to the Docker socket
# goes through the host's docker group (group_add in docker-compose.yml).
USER 1000:1000

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/warden-heartbeat')<300 else 1)"

CMD ["python", "-m", "warden"]
