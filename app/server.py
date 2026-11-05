#!/usr/bin/env python3
import asyncio
import os
import sys
import uuid
import time
import shutil
import subprocess
import traceback
from aiohttp import web, WSMsgType

PORT = int(os.environ.get("PORT", 7860))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SESSIONS_DIR = os.path.join("/tmp", "jhora_sessions")
JHORA_DIR = os.path.join(BASE_DIR, "jhora")

sessions = {}
next_display = 100
last_ws_error = "none"

def get_or_create_session(sid):
    global next_display
    if sid and sid in sessions:
        sessions[sid]["last_active"] = time.time()
        return sessions[sid]

    sid = sid or str(uuid.uuid4())[:8]
    disp = next_display
    next_display += 1
    vnc_p = 5900 + disp

    user_dir = os.path.join(SESSIONS_DIR, sid)
    charts_dir = os.path.join(user_dir, "charts")
    wine_prefix = os.path.join(user_dir, "wineprefix")
    os.makedirs(charts_dir, exist_ok=True)
    os.makedirs(wine_prefix, exist_ok=True)

    # Copy template chart files if any
    sample_data = os.path.join(JHORA_DIR, "data")
    if os.path.exists(sample_data):
        for f in os.listdir(sample_data):
            src = os.path.join(sample_data, f)
            if os.path.isfile(src):
                shutil.copy2(src, charts_dir)

    # Environment for this session
    env = os.environ.copy()
    env["DISPLAY"] = f":{disp}"
    env["WINEPREFIX"] = wine_prefix
    env["WINEDEBUG"] = "-all"

    procs = []

    # 1. Start Xvfb (Virtual Framebuffer Display)
    xvfb_log = open(os.path.join(user_dir, "xvfb.log"), "w")
    xvfb_cmd = ["Xvfb", f":{disp}", "-screen", "0", "1280x800x24", "-ac", "+extension", "RANDR"]
    p_xvfb = subprocess.Popen(xvfb_cmd, env=env, stdout=xvfb_log, stderr=subprocess.STDOUT)
    procs.append(("xvfb", p_xvfb))
    time.sleep(0.8)

    # 2. Start x11vnc
    vnc_log = open(os.path.join(user_dir, "x11vnc.log"), "w")
    x11vnc_cmd = ["x11vnc", "-display", f":{disp}", "-rfbport", str(vnc_p), "-listen", "127.0.0.1", "-forever", "-shared", "-nopw"]
    p_vnc = subprocess.Popen(x11vnc_cmd, env=env, stdout=vnc_log, stderr=subprocess.STDOUT)
    procs.append(("x11vnc", p_vnc))
    time.sleep(1.0)

    # 3. Start JHora under Wine
    jhora_log = open(os.path.join(user_dir, "jhora.log"), "w")
    jhora_exe = os.path.join(JHORA_DIR, "jhora.exe")
    p_jhora = subprocess.Popen(["wine", jhora_exe], cwd=JHORA_DIR, env=env, stdout=jhora_log, stderr=subprocess.STDOUT)
    procs.append(("jhora", p_jhora))

    sess = {
        "sid": sid,
        "display": disp,
        "vnc_port": vnc_p,
        "user_dir": user_dir,
        "charts_dir": charts_dir,
        "procs": procs,
        "last_active": time.time()
    }
    sessions[sid] = sess
    print(f"Created isolated session {sid} on DISPLAY :{disp} (VNC {vnc_p})")
    sys.stdout.flush()
    return sess

async def index_handler(request):
    try:
        sid = request.cookies.get("jhora_sid")
        if not sid or sid not in sessions:
            sid = str(uuid.uuid4())[:8]
        
        get_or_create_session(sid)

        index_path = os.path.join(STATIC_DIR, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()

        response = web.Response(text=html, content_type="text/html")
        response.set_cookie("jhora_sid", sid, max_age=86400 * 30, path="/")
        return response
    except Exception as e:
        tb = traceback.format_exc()
        print("ERROR IN INDEX HANDLER:", tb)
        sys.stdout.flush()
        return web.Response(text=f"Startup Error:\n{tb}", status=500)

async def ws_proxy_handler(request):
    global last_ws_error
    try:
        sid = request.query.get("sid") or request.cookies.get("jhora_sid")
        sess = get_or_create_session(sid)
        
        # Negotiate subprotocol (noVNC requests 'binary')
        requested_protocols = request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        accepted_protocol = None
        for proto in [p.strip() for p in requested_protocols]:
            if proto in ["binary", "base64"]:
                accepted_protocol = proto
                break

        ws_client = web.WebSocketResponse(protocols=[accepted_protocol] if accepted_protocol else None)
        await ws_client.prepare(request)

        # Retry connecting to x11vnc for up to 6 seconds while it finishes initializing
        vnc_reader, vnc_writer = None, None
        for attempt in range(30):
            try:
                vnc_reader, vnc_writer = await asyncio.open_connection("127.0.0.1", sess["vnc_port"])
                break
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.2)

        if not vnc_writer:
            raise Exception(f"Failed to connect to x11vnc on port {sess['vnc_port']} after 6 seconds")

        print(f"Connected to x11vnc TCP port {sess['vnc_port']} for session {sid}")
        sys.stdout.flush()

        async def vnc_to_client():
            try:
                while not ws_client.closed:
                    data = await vnc_reader.read(65536)
                    if not data:
                        break
                    await ws_client.send_bytes(data)
            except Exception:
                pass
            finally:
                if not ws_client.closed:
                    await ws_client.close()

        async def client_to_vnc():
            try:
                async for msg in ws_client:
                    if msg.type == WSMsgType.BINARY:
                        vnc_writer.write(msg.data)
                        await vnc_writer.drain()
                    elif msg.type == WSMsgType.CLOSE:
                        break
            except Exception:
                pass
            finally:
                vnc_writer.close()

        await asyncio.gather(vnc_to_client(), client_to_vnc())
        return ws_client
    except Exception as e:
        last_ws_error = f"{type(e).__name__}: {str(e)}"
        print(f"WS Handler Error: {last_ws_error}")
        sys.stdout.flush()
        return web.Response(status=500)

async def debug_handler(request):
    sid = request.query.get("sid") or request.cookies.get("jhora_sid")
    sess = get_or_create_session(sid)
    status_data = {"sid": sid, "last_ws_error": last_ws_error, "processes": {}, "logs": {}}
    for name, p in sess["procs"]:
        code = p.poll()
        status_data["processes"][name] = {"pid": p.pid, "returncode": code, "running": code is None}
        log_path = os.path.join(sess["user_dir"], f"{name}.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                status_data["logs"][name] = f.read()[-1000:]
    return web.json_response(status_data)

async def list_charts_handler(request):
    sid = request.query.get("sid") or request.cookies.get("jhora_sid")
    sess = get_or_create_session(sid)
    charts = []
    if os.path.exists(sess["charts_dir"]):
        for f in os.listdir(sess["charts_dir"]):
            if f.lower().endswith(".jhd") or f.lower().endswith(".jh"):
                p = os.path.join(sess["charts_dir"], f)
                charts.append({
                    "name": f,
                    "size": os.path.getsize(p),
                    "mtime": os.path.getmtime(p)
                })
    return web.json_response({"charts": charts})

async def download_chart_handler(request):
    sid = request.query.get("sid") or request.cookies.get("jhora_sid")
    file_name = request.query.get("file")
    sess = get_or_create_session(sid)
    file_path = os.path.join(sess["charts_dir"], os.path.basename(file_name))
    if os.path.exists(file_path):
        return web.FileResponse(file_path, headers={
            "Content-Disposition": f'attachment; filename="{os.path.basename(file_name)}"'
        })
    return web.Response(status=404, text="Chart not found")

async def upload_chart_handler(request):
    sid = request.query.get("sid") or request.cookies.get("jhora_sid")
    sess = get_or_create_session(sid)
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename:
            file_name = os.path.basename(part.filename)
            dest = os.path.join(sess["charts_dir"], file_name)
            with open(dest, "wb") as f:
                while True:
                    chunk = await part.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            print(f"Uploaded chart {file_name} for session {sid}")
            sys.stdout.flush()
            return web.json_response({"success": True, "filename": file_name})
    return web.json_response({"success": False, "error": "No file uploaded"})

def cleanup_idle_sessions():
    now = time.time()
    for sid in list(sessions.keys()):
        sess = sessions[sid]
        # Clean up if idle for over 45 minutes
        if now - sess["last_active"] > 2700:
            print(f"Cleaning up idle session: {sid}")
            for name, p in sess["procs"]:
                try:
                    p.terminate()
                except Exception:
                    pass
            del sessions[sid]

async def background_reaper(app):
    while True:
        await asyncio.sleep(60)
        cleanup_idle_sessions()

async def on_startup(app):
    asyncio.create_task(background_reaper(app))

def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", index_handler)
    app.router.add_get("/ws", ws_proxy_handler)
    app.router.add_get("/api/debug", debug_handler)
    app.router.add_get("/api/charts", list_charts_handler)
    app.router.add_get("/api/download", download_chart_handler)
    app.router.add_post("/api/upload", upload_chart_handler)
    app.router.add_static("/static/", STATIC_DIR)
    return app

if __name__ == "__main__":
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    print("=================================================================")
    print(f"Jagannatha Hora Multi-User Gateway starting on port {PORT}...")
    print("=================================================================")
    sys.stdout.flush()
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
