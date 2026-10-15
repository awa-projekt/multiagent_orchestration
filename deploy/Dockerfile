# Reference image for `awa serve`, `awa worker` and `awa mcp serve`.
#
# It installs awa from a wheel you built yourself (`uv build --wheel` writes it to dist/); nothing
# is taken from a public awa package. Build from the repository root:
#
#   docker compose -f deploy/compose.yaml build
#
# Offline build: put the awa wheel and all dependency wheels (a wheelhouse, see
# docs/develop/production.md) into dist/ and build with
#   --build-arg PIP_INSTALL_OPTIONS="--no-index --find-links /tmp/dist"
#
# Pin the base image by digest for repeatable builds (`docker buildx imagetools inspect`).
ARG PYTHON_IMAGE=python:3.13-slim
FROM ${PYTHON_IMAGE}

ARG AWA_EXTRAS=server,postgres,langfuse,anthropic,openai,mcp,m365
ARG PIP_INSTALL_OPTIONS=""

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/awa

COPY dist/ /tmp/dist/
RUN set -eu; \
    count=$(ls /tmp/dist/awa_agents-*.whl 2>/dev/null | wc -l); \
    if [ "$count" -ne 1 ]; then echo "dist/ must hold exactly one awa_agents wheel (found $count)" >&2; exit 1; fi; \
    wheel=$(ls /tmp/dist/awa_agents-*.whl); \
    pip install --no-cache-dir $PIP_INSTALL_OPTIONS "${wheel}[${AWA_EXTRAS}]"; \
    rm -rf /tmp/dist; \
    useradd --uid 10001 --create-home --home-dir /home/awa awa; \
    mkdir -p /srv/project /etc/awa/ca; \
    chown awa:awa /srv/project

COPY deploy/entrypoint.sh /usr/local/bin/awa-entrypoint
RUN chmod 0755 /usr/local/bin/awa-entrypoint

USER awa
WORKDIR /srv/project
ENTRYPOINT ["/usr/local/bin/awa-entrypoint"]
CMD ["awa", "doctor"]
