#!/usr/bin/env python3
import asyncio
import os
import sys
import uuid
import time
import shutil
import subprocess
import json
import mimetypes
from aiohttp import web, WSMsgType
import aiohttp

PORT = int(os.environ.get("PORT", 7860))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SESSIONS_DIR = os.path.join("/tmp", "jhora_sessions")
JHORA_DIR = os.path.join(BASE_DIR, "jhora")

sessions = {}
# session structure:
# {
#   "sid": "...",
#   "display": 100,
#   "vnc_port": 6000,
#   "ws_port": 6100,
#   "user_dir": "...",
#   "charts_dir": "...",
#   "procs": [...],
#   "last_active": timestamp
# }

next_display = 100

def get_or_create_session(sid):
    global next_display
    if sid and sid in sessions:
        sessions[sid]["last_active"] = time.time()
        return sessions[sid]

    sid = sid or str(uuid.uuid4())[:8]
    disp = next_display
    next_display += 1
    vnc_p = 5900 + disp
    ws_p = 7000 + disp

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
    xvfb_cmd = ["Xvfb", f":{disp}", "-screen", "0", "1280x800x24", "+extension", "RANDR", "-noreset"]
    p_xvfb = subprocess.Popen(xvfb_cmd, env=env)
    procs.append(p_xvfb)
    time.sleep(0.5)

    # 2. Start Openbox (lightweight window manager)
    p_ob = subprocess.Popen(["openbox"], env=env)
    procs.append(p_ob)

    # 3. Start x11vnc
    x11vnc_cmd = ["x11vnc", "-display", f":{disp}", "-rfbport", str(vnc_p), "-forever", "-shared", "-nopw", "-quiet"]
    p_vnc = subprocess.Popen(x11vnc_cmd, env=env)
    procs.append(p_vnc)
    time.sleep(0.3)

    # 4. Start websockify for this session
    websockify_cmd = ["websockify", "--web=/usr/share/novnc", str(ws_p), f"127.0.0.1:{vnc_p}"]
    p_ws = subprocess.Popen(websockify_cmd)
    procs.append(p_ws)
    time.sleep(0.3)

    # 5. Start JHora under Wine
    # Create symlink from Z: to charts_dir for convenient opening
    jhora_exe = os.path.join(JHORA_DIR, "jhora.exe")
    p_jhora = subprocess.Popen(["wine", jhora_exe], cwd=JHORA_DIR, env=env)
    procs.append(p_jhora)

    sess = {
        "sid": sid,
        "display": disp,
        "vnc_port": vnc_p,
        "ws_port": ws_p,
        "user_dir": user_dir,
        "charts_dir": charts_dir,
        "procs": procs,
        "last_active": time.time()
    }
    sessions[sid] = sess
    print(f"Created isolated session {sid} on DISPLAY :{disp} (VNC {vnc_p}, WS {ws_p})")
    return sess

async def index_handler(request):
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

async def ws_proxy_handler(request):
    sid = request.query.get("sid") or request.cookies.get("jhora_sid")
    sess = get_or_create_session(sid)
    
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    target_url = f"ws://127.0.0.1:{sess['ws_port']}/websockify"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(target_url) as ws_server:
                async def client_to_server():
                    async for msg in ws_client:
                        if msg.type == WSMsgType.BINARY:
                            await ws_server.send_bytes(msg.data)
                        elif msg.type == WSMsgType.TEXT:
                            await ws_server.send_str(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_server.close()
                            break

                async def server_to_client():
                    async for msg in ws_server:
                        if msg.type == WSMsgType.BINARY:
                            await ws_client.send_bytes(msg.data)
                        elif msg.type == WSMsgType.TEXT:
                            await ws_client.send_str(msg.data)
                        elif msg.type == WSMsgType.CLOSE:
                            await ws_client.close()
                            break

                await asyncio.gather(client_to_server(), server_to_client())
        except Exception as e:
            print(f"WebSocket proxy error for {sid}: {e}")

    return ws_client

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
            return web.json_response({"success": True, "filename": file_name})
    return web.json_response({"success": False, "error": "No file uploaded"})

def cleanup_idle_sessions():
    now = time.time()
    for sid in list(sessions.keys()):
        sess = sessions[sid]
        # Clean up if idle for over 45 minutes
        if now - sess["last_active"] > 2700:
            print(f"Cleaning up idle session: {sid}")
            for p in sess["procs"]:
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
    app.router.add_get("/api/charts", list_charts_handler)
    app.router.add_get("/api/download", download_chart_handler)
    app.router.add_post("/api/upload", upload_chart_handler)
    app.router.add_static("/static/", STATIC_DIR)
    # Serve novnc assets directly if needed
    if os.path.exists("/usr/share/novnc"):
        app.router.add_static("/novnc/", "/usr/share/novnc")
    return app

if __name__ == "__main__":
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)
    print("=================================================================")
    print(f"Jagannatha Hora Multi-User Gateway starting on port {PORT}...")
    print("=================================================================")
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
