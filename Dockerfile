# The base image is pinned by digest (the multi-arch index, so amd64 and arm64
# both resolve). Dependabot bumps it; do not replace it with a bare tag.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

RUN groupadd -g 1001 hleop && useradd -u 1001 -g hleop -m hleop

# Dependencies come from requirements.lock (exported from uv.lock, with hashes),
# never resolved at build time. --require-hashes makes pip refuse anything that
# is not byte-for-byte what the lock names; --no-deps keeps it to exactly that set.
# Regenerate after changing dependencies:
#   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.lock
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

# The operator itself is plain Python on PYTHONPATH: no build backend to fetch.
COPY src/hle_operator /app/hle_operator
RUN python -m compileall -q /app
ENV PYTHONPATH=/app

USER hleop

ENTRYPOINT ["python", "-m", "hle_operator"]
