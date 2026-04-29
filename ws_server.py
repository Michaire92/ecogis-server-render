# -*- coding: utf-8 -*-
"""
EcoGIS Live — Serveur Cloud v2 corrigé
HTTP + WebSocket sur le même port (Render free tier).
"""
import os, json, threading, time, logging
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sock import Sock

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ecogis")

PORT   = int(os.environ.get('PORT', 10000))
SECRET = os.environ.get('ECOGIS_SECRET', 'pendjari2026')

app  = Flask(__name__)
sock = Sock(app)
CORS(app)

points_store   = []
qgis_clients   = set()
mobile_clients = set()
lock           = threading.Lock()

@app.route('/')
@app.route('/form')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/status')
def status():
    with lock:
        n = len(points_store)
    return jsonify({
        'status': 'ok', 'server': 'EcoGIS Live',
        'version': '3.1', 'points': n,
        'qgis': len(qgis_clients),
        'mobile': len(mobile_clients),
        'ts': datetime.utcnow().isoformat()
    })

@app.route('/api/points')
def get_points():
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        pts = list(points_store)
    since = request.args.get('since')
    if since:
        pts = [p for p in pts if p.get('ts','') > since]
    return jsonify({'points': pts, 'count': len(pts)})

@sock.route('/ws')
def ws_handler(ws):
    role = None
    try:
        # Identification — timeout généreux pour Render
        try:
            raw = ws.receive(timeout=30)
        except Exception:
            return

        if not raw:
            return

        try:
            msg = json.loads(raw)
        except Exception:
            ws.send(json.dumps({"ok": False, "error": "JSON invalide"}))
            return

        # Vérifier type hello
        if msg.get('type') != 'hello':
            ws.send(json.dumps({"ok": False, "error": "Envoyez d'abord un message hello"}))
            return

        # Vérifier secret
        client_secret = msg.get('secret', '')
        if client_secret != SECRET:
            log.warning(f"[WS] Secret invalide reçu: '{client_secret}' (attendu: '{SECRET}')")
            ws.send(json.dumps({"ok": False, "error": "Mot de passe incorrect"}))
            return

        role = msg.get('role', 'mobile')
        ws.send(json.dumps({
            "ok": True,
            "role": role,
            "msg": f"Connecté à EcoGIS Live ✓ ({role})"
        }))
        log.info(f"[WS] Nouveau {role} connecté")

        if role == 'qgis':
            with lock:
                qgis_clients.add(ws)
                existing = list(points_store)
            if existing:
                ws.send(json.dumps({
                    "type": "bulk",
                    "points": existing,
                    "count": len(existing)
                }))
        else:
            with lock:
                mobile_clients.add(ws)

        # Boucle lecture
        while True:
            try:
                raw = ws.receive(timeout=120)
            except Exception:
                break

            if raw is None:
                break

            try:
                data = json.loads(raw)
            except Exception:
                ws.send(json.dumps({"ok": False, "error": "JSON invalide"}))
                continue

            if role == 'mobile' and data.get('type') == 'observation':
                data['server_ts'] = datetime.utcnow().isoformat()
                with lock:
                    points_store.append(data)
                    n = len(points_store)
                ws.send(json.dumps({"ok": True, "id": n}))
                log.info(f"[OBS] #{n} {data.get('species','?')} @ {data.get('lat')},{data.get('lon')}")

                with lock:
                    qgis_copy = set(qgis_clients)
                payload = json.dumps({"type": "observation", **data})
                dead = set()
                for qws in qgis_copy:
                    try:
                        qws.send(payload)
                    except Exception:
                        dead.add(qws)
                if dead:
                    with lock:
                        qgis_clients -= dead

            elif role == 'qgis':
                cmd = data.get('cmd')
                if cmd == 'ping':
                    ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                elif cmd == 'clear' and data.get('secret') == SECRET:
                    with lock:
                        points_store.clear()
                    ws.send(json.dumps({"ok": True, "cmd": "clear"}))

    except Exception as e:
        log.warning(f"[WS] Erreur ({role}): {e}")
    finally:
        if role == 'qgis':
            with lock:
                qgis_clients.discard(ws)
        elif role == 'mobile':
            with lock:
                mobile_clients.discard(ws)
        log.info(f"[WS] Déconnexion: {role}")

if __name__ == '__main__':
    log.info(f"EcoGIS Live v3.1 sur port {PORT} — SECRET={'*'*len(SECRET)}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
