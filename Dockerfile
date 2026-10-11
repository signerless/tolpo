# syntax=docker/dockerfile:1

FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim
LABEL org.opencontainers.image.title="tolpo" \
      org.opencontainers.image.description="Local-first MCP server and HTTP API that let AI agents read and control TP-Link Tapo smart-home devices" \
      org.opencontainers.image.source="https://github.com/signerless/tolpo" \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.signerless/tolpo"
COPY --from=build /install /usr/local
RUN useradd --create-home --uid 10001 tolpo
USER tolpo
WORKDIR /home/tolpo
EXPOSE 8765
# `mcp` speaks the MCP protocol on stdin and stdout. Pass `serve --host 0.0.0.0` for the HTTP API,
# which also needs a token in TOLPO_TOKEN because it is no longer bound to loopback.
ENTRYPOINT ["tolpo"]
CMD ["mcp"]
