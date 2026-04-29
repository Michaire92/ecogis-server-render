# -*- coding: utf-8 -*-
"""EcoGIS Live — Serveur Cloud v3 — corrigé"""
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
        'version': '3.2', 'points': n,
        'ts': datetime.utcnow().isoformat()
    })

@app.route('/api/points')
def get_points():
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        pts = list(points_store)
    return jsonify({'points': pts, 'count': len(pts)})

@sock.route('/ws')
def ws_handler(ws):
    # Déclarer les globales explicitement
    global qgis_clients, mobile_clients, points_store

    role = None
    try:
        # Identification
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

        if msg.get('type') != 'hello':
            ws.send(json.dumps({"ok": False, "error": "Message hello attendu"}))
            return

        client_secret = str(msg.get('secret', ''))
        server_secret = str(SECRET)

        if client_secret != server_secret:
            log.warning(f"[WS] Secret invalide: recu='{client_secret}' attendu='{server_secret}'")
            ws.send(json.dumps({"ok": False, "error": "Mot de passe incorrect"}))
            return

        role = str(msg.get('role', 'mobile'))
        ws.send(json.dumps({"ok": True, "role": role, "msg": f"Connecte ({role})"}))
        log.info(f"[WS] Connecte: {role}")

        # Enregistrer le client
        with lock:
            if role == 'qgis':
                qgis_clients.add(ws)
                existing = list(points_store)
            else:
                mobile_clients.add(ws)
                existing = []

        # Envoyer les points existants au plugin QGIS
        if existing:
            ws.send(json.dumps({"type": "bulk", "points": existing, "count": len(existing)}))

        # Boucle de lecture
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
                log.info(f"[OBS] #{n} {data.get('species','?')}")

                # Diffuser à QGIS
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
                if data.get('cmd') == 'ping':
                    ws.send(json.dumps({"type": "pong", "ts": time.time()}))

    except Exception as e:
        log.warning(f"[WS] Erreur ({role}): {e}")
    finally:
        # Nettoyage — utiliser global explicitement
        with lock:
            if role == 'qgis':
                qgis_clients.discard(ws)
            elif role == 'mobile':
                mobile_clients.discard(ws)
        log.info(f"[WS] Deconnexion: {role}")

if __name__ == '__main__':
    log.info(f"EcoGIS Live v3.2 port={PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
