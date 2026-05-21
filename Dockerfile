FROM python:3.12-slim

RUN groupadd -g 1001 hleop && useradd -u 1001 -g hleop -m hleop

COPY pyproject.toml /src/pyproject.toml
COPY src/ /src/src/

RUN pip install --no-cache-dir /src

USER hleop

ENTRYPOINT ["kopf", "run", "/src/src/hle_operator/handlers.py", "--liveness=http://0.0.0.0:8080/healthz", "--verbose"]
