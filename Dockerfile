FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=7860
ENV WINEARCH=win32

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine32 \
        xvfb \
        openbox \
        x11vnc \
        novnc \
        websockify \
        python3 \
        python3-pip \
        python3-aiohttp \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
        procps \
        curl && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user
ENV PATH=/home/user/.local/bin:$PATH

WORKDIR /app

COPY --chown=user:user app /app

EXPOSE 7860

CMD ["python3", "/app/server.py"]
