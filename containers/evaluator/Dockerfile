FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

LABEL org.opencontainers.image.title="Guildmind fixture evaluator"
LABEL org.opencontainers.image.description="Fixed, offline evaluator harness for Stage 1 fixtures"
LABEL org.opencontainers.image.source="https://github.com/jeremymatthewwerner/guildmind"

ENV LANG=C \
    LC_ALL=C \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONNOUSERSITE=1 \
    TZ=UTC

COPY --chmod=0555 evaluate.py /opt/guildmind/evaluate.py

USER 65532:65532
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/python", "/opt/guildmind/evaluate.py"]
