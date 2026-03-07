FROM python:3.12-slim

RUN groupadd -r operator && useradd -r -g operator operator

COPY pyproject.toml /src/pyproject.toml
COPY src/ /src/src/

RUN pip install --no-cache-dir /src

USER operator

ENTRYPOINT ["kopf", "run", "/src/src/hle_operator/handlers.py", "--liveness=http://0.0.0.0:8080/healthz", "--verbose"]
