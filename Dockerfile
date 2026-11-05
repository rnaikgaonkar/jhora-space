FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=7860

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine \
        wine64 \
        wine32 \
        xvfb \
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

RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

ENV WINEPREFIX=/root/.wine
ENV WINEDEBUG=-all

WORKDIR /app

COPY app /app

RUN ln -s /app/jhora/jhcore /app/jhcore && ln -s /app/jhora/data /app/data

EXPOSE 7860

CMD ["python3", "/app/server.py"]
