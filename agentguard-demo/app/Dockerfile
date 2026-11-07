# AURA (mezmo/aura v0.2.20 CLI) as a Jozu Agent Guard custom runtime.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl procps \
 && rm -rf /var/lib/apt/lists/*
COPY aura-linux-arm64 /usr/local/bin/aura
COPY aura.toml entrypoint.sh /app/
RUN chmod +x /usr/local/bin/aura /app/entrypoint.sh
LABEL dev.jozu.agentguard.min-version=0.11.0
ENV SRE_MODEL=kimi
ENTRYPOINT ["/app/entrypoint.sh"]
