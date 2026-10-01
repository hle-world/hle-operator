FROM python:3.12-slim

RUN groupadd -g 1001 hleop && useradd -u 1001 -g hleop -m hleop

COPY pyproject.toml /src/pyproject.toml
COPY README.md /src/README.md
COPY src/ /src/src/

RUN pip install --no-cache-dir /src

USER hleop

ENTRYPOINT ["python", "-m", "hle_operator"]
