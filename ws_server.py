# -*- coding: utf-8 -*-
"""
EcoGIS Live — Serveur Cloud v4
- Points avec ID unique (UUID)
- Pas de renvoi automatique des anciens points au plugin
- API REST complète : lister, supprimer, modifier un point
- Zéro modification nécessaire après déploiement
"""
import os, json, threading, time, logging, uuid
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

points_store   = {}
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
    return jsonify({'status': 'ok', 'version': '4.0', 'points': n, 'qgis': len(qgis_clients), 'ts': datetime.utcnow().isoformat()})

@app.route('/api/points')
def get_points():
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        pts = list(points_store.values())
    since = request.args.get('since')
    if since:
        pts = [p for p in pts if p.get('server_ts', '') > since]
    return jsonify({'points': pts, 'count': len(pts)})

@app.route('/api/points/<uid>', methods=['DELETE'])
def delete_point(uid):
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        if uid not in points_store:
            return jsonify({'error': 'Point introuvable'}), 404
        del points_store[uid]
    _broadcast_qgis(json.dumps({'type': 'delete', 'uid': uid}))
    return jsonify({'ok': True, 'uid': uid})

@app.route('/api/points/<uid>', methods=['PUT'])
def update_point(uid):
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        if uid not in points_store:
            return jsonify({'error': 'Point introuvable'}), 404
        data = request.get_json(silent=True) or {}
        for field in ('species', 'count', 'category', 'status', 'sex', 'behavior', 'cam_id', 'note'):
            if field in data:
                points_store[uid][field] = data[field]
        points_store[uid]['updated_ts'] = datetime.utcnow().isoformat()
        updated = dict(points_store[uid])
    _broadcast_qgis(json.dumps({'type': 'update', 'point': updated}))
    return jsonify({'ok': True, 'point': updated})

@app.route('/api/points', methods=['DELETE'])
def clear_points():
    if request.args.get('secret') != SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    with lock:
        points_store.clear()
    _broadcast_qgis(json.dumps({'type': 'clear_all'}))
    return jsonify({'ok': True})

def _broadcast_qgis(payload):
    with lock:
        qgis_copy = set(qgis_clients)
    dead = set()
    for qws in qgis_copy:
        try:
            qws.send(payload)
        except Exception:
            dead.add(qws)
    if dead:
        with lock:
            qgis_clients -= dead

@sock.route('/ws')
def ws_handler(ws):
    global qgis_clients, mobile_clients, points_store
    role = None
    try:
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
        if str(msg.get('secret', '')) != str(SECRET):
            ws.send(json.dumps({"ok": False, "error": "Mot de passe incorrect"}))
            return
        role = str(msg.get('role', 'mobile'))
        want_history = bool(msg.get('want_history', False))
        ws.send(json.dumps({"ok": True, "role": role, "msg": f"Connecté ({role})"}))
        log.info(f"[WS] Connecté: {role} | history={want_history}")
        with lock:
            if role == 'qgis':
                qgis_clients.add(ws)
                existing = list(points_store.values()) if want_history else []
            else:
                mobile_clients.add(ws)
                existing = []
        if existing:
            ws.send(json.dumps({"type": "bulk", "points": existing, "count": len(existing)}))
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
                point_uid = str(uuid.uuid4())
                data['uid']       = point_uid
                data['server_ts'] = datetime.utcnow().isoformat()
                with lock:
                    points_store[point_uid] = data
                ws.send(json.dumps({"ok": True, "uid": point_uid}))
                log.info(f"[OBS] #{len(points_store)} {data.get('species','?')} uid={point_uid[:8]}")
                _broadcast_qgis(json.dumps({"type": "observation", **data}))
            elif role == 'qgis':
                cmd = data.get('cmd')
                if cmd == 'ping':
                    ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                elif cmd == 'delete':
                    uid = data.get('uid')
                    with lock:
                        if uid and uid in points_store:
                            del points_store[uid]
                    ws.send(json.dumps({"ok": True, "cmd": "delete", "uid": uid}))
                    _broadcast_qgis(json.dumps({"type": "delete", "uid": uid}))
                elif cmd == 'update':
                    uid = data.get('uid')
                    with lock:
                        if uid and uid in points_store:
                            for f in ('species','count','category','status','sex','behavior','cam_id','note'):
                                if f in data:
                                    points_store[uid][f] = data[f]
                            points_store[uid]['updated_ts'] = datetime.utcnow().isoformat()
                            updated = dict(points_store[uid])
                        else:
                            updated = None
                    if updated:
                        ws.send(json.dumps({"ok": True, "cmd": "update", "point": updated}))
                        _broadcast_qgis(json.dumps({"type": "update", "point": updated}))
                elif cmd == 'clear_all' and data.get('secret') == SECRET:
                    with lock:
                        points_store.clear()
                    ws.send(json.dumps({"ok": True, "cmd": "clear_all"}))
                    _broadcast_qgis(json.dumps({"type": "clear_all"}))
                elif cmd == 'get_history':
                    with lock:
                        pts = list(points_store.values())
                    ws.send(json.dumps({"type": "bulk", "points": pts, "count": len(pts)}))
    except Exception as e:
        log.warning(f"[WS] Erreur ({role}): {e}")
    finally:
        with lock:
            if role == 'qgis':
                qgis_clients.discard(ws)
            elif role == 'mobile':
                mobile_clients.discard(ws)
        log.info(f"[WS] Déconnexion: {role}")

if __name__ == '__main__':
    log.info(f"EcoGIS Live v4.0 port={PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=False)
