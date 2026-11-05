FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=7860
ENV WINEARCH=win32

RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine \
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

# Pre-initialize Wine prefix once during build so container starts instantly
RUN wineboot -i && wineserver -w


WORKDIR /app

COPY app /app

EXPOSE 7860

CMD ["python3", "/app/server.py"]
