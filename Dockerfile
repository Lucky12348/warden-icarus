# Binaires statiques du client Docker et du plugin compose (pour recréer le conteneur Icarus).
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

# Jamais root : uid 1000 (propriétaire des fichiers d'Icarus) ; l'accès au socket Docker
# passe par le groupe docker de l'hôte (group_add dans docker-compose.yml).
USER 1000:1000

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/warden-heartbeat')<300 else 1)"

CMD ["python", "-m", "warden"]
