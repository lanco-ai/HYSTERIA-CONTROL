#!/usr/bin/env python3
import html
import base64
import hashlib
import hmac
import json
import fcntl
import re
import secrets
import time
import uuid
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

USERS_FILE = Path('/root/hysteria/users.json')
USAGE_FILE = Path('/root/hysteria/state/usage.json')
ONLINE_FILE = Path('/root/hysteria/state/online.json')
META_FILE = Path('/root/hysteria/subscription_meta.json')
TEMPLATE_FILE = Path('/root/hysteria/template.yaml')
SESSIONS_FILE = Path('/root/hysteria/state/panel_sessions.json')
RESET_LOG_FILE = Path('/root/hysteria/state/usage_reset.log')
USAGE_LOCK_FILE = Path('/root/hysteria/state/usage.lock')
HY_API_BASE = 'http://127.0.0.1:25413'
HY_API_SECRET = '04a1533b4423ff31252a9b4b74ca85ae309399c0e3ef7688'


XRAY_CONFIG_FILE = Path('/usr/local/etc/xray/config.json')
XRAY_INBOUND_PORTS = (443, 8443)
XRAY_BACKUP_SUFFIX = '-backup'


def _xray_email_for(port, username):
    return username if port == 443 else f'{username}{XRAY_BACKUP_SUFFIX}'


def xray_sync_user(username, vless_uuid):
    """Ensure username is present in every vless inbound with the given uuid. Returns True if file changed."""
    try:
        cfg = json.loads(XRAY_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception:
        return False
    changed = False
    for ib in cfg.get('inbounds') or []:
        if ib.get('protocol') != 'vless':
            continue
        port = ib.get('port')
        if port not in XRAY_INBOUND_PORTS:
            continue
        clients = ib.setdefault('settings', {}).setdefault('clients', [])
        email = _xray_email_for(port, username)
        found = None
        for c in clients:
            if c.get('email') == email:
                found = c
                break
        if found is None:
            clients.append({'id': vless_uuid, 'email': email, 'flow': 'xtls-rprx-vision'})
            changed = True
        elif found.get('id') != vless_uuid or found.get('flow') != 'xtls-rprx-vision':
            found['id'] = vless_uuid
            found['flow'] = 'xtls-rprx-vision'
            changed = True
    if changed:
        XRAY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return changed


def xray_remove_user(username):
    try:
        cfg = json.loads(XRAY_CONFIG_FILE.read_text(encoding='utf-8'))
    except Exception:
        return False
    changed = False
    targets = {_xray_email_for(p, username) for p in XRAY_INBOUND_PORTS}
    for ib in cfg.get('inbounds') or []:
        if ib.get('protocol') != 'vless':
            continue
        clients = ib.get('settings', {}).get('clients') or []
        new_clients = [c for c in clients if c.get('email') not in targets]
        if len(new_clients) != len(clients):
            ib['settings']['clients'] = new_clients
            changed = True
    if changed:
        XRAY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return changed


def xray_reload_async():
    """Restart xray without blocking the HTTP response or tying to the admin's ssh session."""
    try:
        import subprocess
        subprocess.Popen(
            ['systemd-run', '--no-block', '--unit', f'xray-reload-{int(time.time())}',
             'systemctl', 'restart', 'xray'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def hy_kick(usernames):
    """Force-disconnect active hysteria sessions for the given usernames."""
    if not usernames:
        return
    try:
        body = json.dumps(list(usernames)).encode('utf-8')
        req = urllib.request.Request(
            f'{HY_API_BASE}/kick',
            data=body,
            headers={'Authorization': HY_API_SECRET, 'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=3):
            return
    except Exception:
        pass
LISTEN = ('127.0.0.1', 8081)
SESSION_TTL = 86400

BASE_CSS = """:root {
  color-scheme: dark;
  --neon-blue: #00f0ff;
  --neon-cyan: #00eaff;
  --neon-purple: #a855f7;
  --neon-pink: #ff0080;
  --dark-bg: #0a0a0f;
  --dark-bg-2: #08111e;
  --card-bg: rgba(20, 20, 30, 0.82);
  --card-bg-strong: rgba(13, 18, 32, 0.9);
  --border-color: rgba(0, 240, 255, 0.30);
  --border-strong: rgba(0, 240, 255, 0.58);
  --text: #ffffff;
  --muted: rgba(255, 255, 255, 0.62);
  --faint: rgba(255, 255, 255, 0.42);
  --danger: #ff5f6d;
  --success: #42f5b0;
  --warning: #fbbf24;
  --radius: 24px;
  --glow: 0 0 20px rgba(0, 240, 255, 0.46), inset 0 0 22px rgba(0, 240, 255, 0.08);
  --soft-shadow: 0 20px 60px rgba(0,0,0,.42), 0 0 34px rgba(0,240,255,.08);
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { min-height: 100%; background: var(--dark-bg); }
body {
  min-height: 100vh;
  margin: 0;
  font-family: Inter, "Noto Sans SC", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: var(--dark-bg);
  color: var(--text);
  padding: 20px;
  position: relative;
  overflow-x: hidden;
  -webkit-font-smoothing: antialiased;
  text-rendering: geometricPrecision;
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(0, 240, 255, 0.105) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 240, 255, 0.105) 1px, transparent 1px),
    radial-gradient(circle at 50% -8%, rgba(0,240,255,.18), transparent 34%),
    radial-gradient(circle at 76% 18%, rgba(168,85,247,.14), transparent 30%);
  background-size: 50px 50px, 50px 50px, 100% 100%, 100% 100%;
  animation: gridMove 20s linear infinite;
  pointer-events: none;
  z-index: 0;
}
body::after {
  content: "";
  position: fixed;
  top: -50%; left: -50%;
  width: 200%; height: 200%;
  background: radial-gradient(circle at 50% 50%, rgba(168,85,247,.15) 0%, rgba(0,240,255,.10) 25%, transparent 50%);
  animation: rotate 30s linear infinite;
  pointer-events: none;
  z-index: 0;
}
body > * { position: relative; z-index: 1; }
.scanline {
  position: fixed; inset: 0; pointer-events: none; z-index: 2; opacity: .16;
  background: repeating-linear-gradient(0deg, transparent 0 2px, rgba(0,240,255,.18) 3px, transparent 4px);
  mix-blend-mode: screen;
}
@keyframes gridMove { from { background-position: 0 0,0 0,0 0,0 0; } to { background-position: 50px 50px,50px 50px,0 0,0 0; } }
@keyframes rotate { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
@keyframes fadeInDown { from { opacity: 0; transform: translateY(-18px); } to { opacity: 1; transform: none; } }
@keyframes shimmer { from { transform: translateX(-120%); } to { transform: translateX(120%); } }
@keyframes pulseGlow { 0%,100% { box-shadow: 0 0 14px rgba(0,240,255,.32); } 50% { box-shadow: 0 0 26px rgba(168,85,247,.36); } }
@keyframes flow { 0%,100% { background-position: 0% 50%; } 50% { background-position: 100% 50%; } }
a { color: var(--neon-blue); text-decoration: none; transition: all .25s ease; }
a:hover { color: #fff; text-shadow: 0 0 16px rgba(0,240,255,.58); }
.wrap, .container { width: min(1000px, 100%); margin: 0 auto; position: relative; z-index: 1; }
.wrap { width: min(1180px, 100%); padding: 8px 0 44px; }
.wrap.narrow { width: min(1000px, 100%); }
.nav { display:none; }
.header, .admin-header {
  text-align: center;
  margin: 14px 0 32px;
  animation: fadeInDown .8s ease;
}
.header h1, .admin-header h1, .hero, .admin-title, .login-title {
  font-size: clamp(38px, 6.2vw, 64px);
  line-height: 1.16;
  font-weight: 800;
  letter-spacing: .025em;
  background: linear-gradient(135deg, var(--neon-blue), var(--neon-purple));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  text-shadow: 0 0 30px rgba(0,240,255,.5);
  margin: 0 0 14px;
}
.header p, .admin-header p, .lede, .login-subtitle {
  color: var(--muted);
  font-size: clamp(14px, 1.8vw, 18px);
  letter-spacing: .08em;
  line-height: 1.75;
}
.eyebrow, .k {
  color: var(--neon-blue);
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .18em;
  text-transform: uppercase;
  text-shadow: 0 0 15px rgba(0,240,255,.45);
}
.nav-links, .toolbar {
  display: flex;
  justify-content: center;
  gap: 18px;
  flex-wrap: wrap;
  margin-bottom: 32px;
  animation: fadeIn 1s ease .15s backwards;
}
.nav-link, .btn.secondary, .toolbar .btn {
  padding: 12px 26px;
  min-height: 44px;
  background: var(--card-bg);
  border: 2px solid var(--border-color);
  border-radius: 999px;
  color: rgba(255,255,255,.82);
  font-weight: 700;
  text-decoration: none;
  transition: all .3s ease;
  backdrop-filter: blur(10px);
  position: relative;
  overflow: hidden;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
}
.nav-link::before, .btn.secondary::before, .toolbar .btn::before {
  content: ""; position: absolute; top: 0; left: -110%; width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, rgba(0,240,255,.30), transparent);
  transition: left .55s ease;
}
.nav-link:hover::before, .btn.secondary:hover::before, .toolbar .btn:hover::before { left: 110%; }
.nav-link:hover, .nav-link.active, .btn.secondary:hover, .toolbar .btn:hover {
  border-color: var(--neon-blue);
  color: var(--neon-blue);
  box-shadow: 0 0 20px rgba(0,240,255,.48), inset 0 0 20px rgba(0,240,255,.10);
  transform: translateY(-2px);
}
.progress-bar, .bar {
  width: 100%; height: 6px; background: rgba(255,255,255,.10); border-radius: 999px;
  margin: 0 0 34px; overflow: hidden; position: relative; border: 0;
}
.progress-fill, .fill, .mini-fill {
  height: 100%; min-width: 4px; border-radius: inherit;
  background: linear-gradient(90deg, var(--neon-blue), var(--neon-purple), var(--neon-pink));
  background-size: 190% 100%;
  box-shadow: 0 0 15px var(--neon-blue);
  animation: flow 5s ease-in-out infinite;
  position: relative;
}
.progress-fill::after, .fill::after {
  content: ""; position:absolute; inset:0; background:linear-gradient(90deg, transparent, rgba(255,255,255,.32), transparent); animation: shimmer 2s infinite;
}
.steps-container, .stats-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 28px;
  animation: fadeIn 1s ease .25s backwards;
}
.step-indicator, .stat-card {
  text-align: center;
  padding: 20px 14px;
  background: var(--card-bg);
  border: 1px solid var(--border-color);
  border-radius: 18px;
  backdrop-filter: blur(10px);
  transition: all .3s ease;
  min-height: 122px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;
}
.step-indicator:hover, .stat-card:hover, .card:hover, .hero-panel:hover {
  transform: translateY(-4px);
  border-color: var(--neon-blue);
  box-shadow: 0 0 25px rgba(0,240,255,.34), inset 0 0 20px rgba(0,240,255,.08);
}
.step-number, .v.big {
  width: auto; height: auto; margin: 0 auto;
  font-size: clamp(24px, 3.5vw, 40px);
  font-weight: 800;
  color: var(--neon-blue);
  text-shadow: 0 0 18px rgba(0,240,255,.68);
  line-height: 1.05;
}
.step-label, .small { color: var(--muted); font-size: 13px; line-height: 1.65; }
.card-wrapper, .card, .hero-panel, .login-card {
  background: var(--card-bg);
  border: 1px solid var(--border-color);
  border-radius: var(--radius);
  backdrop-filter: blur(15px);
  box-shadow: var(--soft-shadow);
  transition: all .3s ease;
  position: relative;
  overflow: hidden;
}
.card-wrapper::before, .card::before, .hero-panel::before, .login-card::before {
  content: ""; position: absolute; inset: 0 0 auto 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--neon-blue), transparent);
  opacity: .75;
}
.card, .hero-panel { padding: 24px; }
.card-wrapper { padding: clamp(24px, 4vw, 42px); animation: fadeIn 1s ease .35s backwards; }
.grid { display: grid; gap: 18px; }
.grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.hero-panel { margin-bottom: 22px; text-align: center; padding: 36px 34px; }
.home-shell { min-height: calc(100vh - 40px); display:flex; flex-direction:column; justify-content:center; }
.center-shell { min-height: calc(100vh - 40px); display:flex; flex-direction:column; justify-content:center; padding-block: clamp(24px, 5vh, 72px); }
.hero-row { display: flex; justify-content: space-between; align-items: center; gap: 22px; }
.admin-hero-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: center; margin: 22px 0 0; }
.admin-hero-actions .btn { min-height: 42px; padding: 11px 22px; }
.badge, .ip-pill {
  display: inline-flex; align-items: center; gap: 8px; width: max-content;
  margin: 0 auto;
  padding: 8px 14px;
  border-radius: 999px;
  border: 1px solid rgba(0,240,255,.34);
  background: rgba(14,18,28,.78);
  color: rgba(255,255,255,.8);
  font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  box-shadow: 0 0 18px rgba(0,240,255,.18), inset 0 1px 0 rgba(255,255,255,.05);
  backdrop-filter: blur(12px);
}
.badge::before, .ip-pill::before { content:""; width:7px; height:7px; border-radius:50%; background:var(--success); box-shadow:0 0 12px rgba(66,245,176,.88); }
.btn {
  min-height: 46px;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  padding: 13px 26px;
  border: none;
  border-radius: 999px;
  cursor: pointer;
  font-weight: 800;
  color: #06131f;
  background: linear-gradient(135deg, var(--neon-blue), var(--neon-purple));
  box-shadow: 0 0 20px rgba(0,240,255,.50);
  text-decoration: none;
  transition: all .3s ease;
  position: relative;
  overflow: hidden;
}
.btn::before { content:""; position:absolute; top:0; left:-100%; width:100%; height:100%; background:linear-gradient(90deg, transparent, rgba(255,255,255,.38), transparent); transition:left .5s ease; }
.btn:hover::before { left:100%; }
.btn:hover { transform: translateY(-2px); box-shadow:0 0 30px rgba(0,240,255,.72); color:#020812; }
.btn.danger { background: rgba(255,95,109,.13) !important; color:#ffdce1 !important; border:1px solid rgba(255,95,109,.48) !important; box-shadow:0 0 18px rgba(255,95,109,.22) !important; }
.btn.danger:hover { background: rgba(255,95,109,.22) !important; box-shadow:0 0 24px rgba(255,95,109,.38) !important; color:#fff !important; }
.inline-form input, .inline-form select, .inline-form textarea, textarea, select, input {
  width: 100%; min-height: 52px; padding: 15px 16px;
  border: 2px solid rgba(0,240,255,.22);
  border-radius: 12px;
  background: rgba(255,255,255,.05);
  color: #fff;
  font-size: 15px;
  line-height: 1.35;
  outline: none;
  transition: all .3s ease;
}
.inline-form input::placeholder, .inline-form textarea::placeholder, input::placeholder, textarea::placeholder { color: rgba(255,255,255,.34); }
.inline-form input:focus, .inline-form select:focus, .inline-form textarea:focus, textarea:focus, select:focus, input:focus {
  border-color: var(--neon-blue);
  box-shadow: 0 0 20px rgba(0,240,255,.34), inset 0 0 14px rgba(0,240,255,.05);
  background: rgba(0,240,255,.07);
}
.inline-form label { display:block; margin: 0 0 10px; color: rgba(255,255,255,.82); font-size: 14px; font-weight: 700; line-height:1.5; }
.row { display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
label.switch { display:inline-flex; align-items:center; gap:8px; color:rgba(255,255,255,.72); }
label.switch input { width:auto; min-height:auto; accent-color:var(--neon-blue); }
.table { width:100%; border-collapse:separate; border-spacing:0 10px; font-size:14px; }
.table th, .table td { padding:16px 14px; text-align:left; vertical-align:top; }
.table th { color:var(--neon-blue); font-size:12px; text-transform:uppercase; letter-spacing:.14em; font-weight:800; border-bottom:1px solid rgba(0,240,255,.2); }
.table tbody tr { background: rgba(255,255,255,.035); transition: all .25s ease; }
.table tbody tr:hover { background: rgba(0,240,255,.07); box-shadow:0 0 18px rgba(0,240,255,.13); }
.table tbody td:first-child { border-radius:14px 0 0 14px; }
.table tbody td:last-child { border-radius:0 14px 14px 0; }
.mini-bar { height:7px; border-radius:999px; background:rgba(255,255,255,.10); overflow:hidden; margin:10px 0 7px; }
.fill.danger, .mini-fill.danger { background: linear-gradient(90deg, var(--warning), var(--danger), var(--neon-pink)); }
code, .mono { font-family: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; background:rgba(0,240,255,.07); border:1px solid rgba(0,240,255,.22); border-radius:12px; padding:10px 12px; color:#dffcff; word-break:break-all; }
.flash, .err { border-radius:16px; padding:14px 16px; margin-bottom:18px; backdrop-filter:blur(12px); animation: fadeIn .4s ease, flash-fade 4s forwards; }
.flash { background:rgba(66,245,176,.12); border:1px solid rgba(66,245,176,.36); color:#caffea; box-shadow:0 0 18px rgba(66,245,176,.12); }
.err { background:rgba(255,95,109,.12); border:1px solid rgba(255,95,109,.36); color:#ffd3d9; box-shadow:0 0 18px rgba(255,95,109,.12); }
@keyframes flash-fade { 0%, 70% { opacity: 1; } 100% { opacity: 0; } }
details > summary { cursor:pointer; color:var(--neon-blue); font-weight:800; list-style:none; }
details > summary::before { content:'▶ '; font-size:10px; }
details[open] > summary::before { content:'▼ '; }
.login-shell { min-height: calc(100vh - 40px); display:flex; align-items:center; justify-content:center; }
.login-card { width:min(520px, 100%); padding: 44px; animation: fadeIn 1s ease .2s backwards; }
.login-top { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:28px; }
.login-mark { width:62px; height:62px; border-radius:50%; display:grid; place-items:center; background:linear-gradient(135deg, var(--neon-blue), var(--neon-purple), var(--neon-pink)); box-shadow:0 0 28px rgba(0,240,255,.45); animation:pulseGlow 3s ease-in-out infinite; }
.login-mark::after { content:'HY'; color:#070b12; font-weight:900; letter-spacing:-.08em; }
.login-title { font-size: clamp(34px, 7vw, 50px); letter-spacing:.015em; line-height:1.18; }
.login-subtitle { letter-spacing: normal; margin-bottom: 26px; }
.action-card { display:flex; flex-direction:column; gap:8px; }
.admin-head { margin-bottom: 26px; }
.admin-title { font-size: clamp(42px, 7vw, 70px); }
.section-gap { margin-top: 22px; }
@media (max-width: 980px) { .grid-4, .grid-3, .grid-2, .steps-container, .stats-strip { grid-template-columns: 1fr 1fr; } .hero-row { flex-direction:column; text-align:center; } }
@media (max-width: 640px) { body { padding:14px; } .grid-4, .grid-3, .grid-2, .steps-container, .stats-strip { grid-template-columns:1fr; } .card-wrapper, .card, .hero-panel, .login-card { padding:22px; } .table { min-width:760px; } .row, .toolbar, .nav-links { flex-direction:column; align-items:stretch; } .btn { width:100%; } .login-top { flex-direction:column; } .header, .admin-header { margin-top:10px; } .hero { font-size: clamp(32px, 11vw, 44px); } .home-shell, .login-shell { min-height: calc(100vh - 28px); } }
@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation:none !important; transition:none !important; } }
"""


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding='utf-8')


@contextmanager
def usage_lock():
    USAGE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOCK_FILE.open('a+', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def parse_int_field(raw, default, min_value, max_value):
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def sanitize_host(raw_host):
    h = (raw_host or '').strip()
    if not h:
        return '127.0.0.1'
    if ',' in h:
        h = h.split(',', 1)[0].strip()
    if '/' in h or '\\' in h or '@' in h:
        return '127.0.0.1'
    if h.count(':') <= 1 and ':' in h:
        name, port = h.rsplit(':', 1)
        if name and port.isdigit() and 1 <= int(port) <= 65535:
            h = name
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-[]:')
    if any(ch not in allowed for ch in h):
        return '127.0.0.1'
    return h or '127.0.0.1'


def safe_base_url(host, forwarded_proto):
    scheme = (forwarded_proto or 'http').split(',')[0].strip().lower()
    if scheme not in ('http', 'https'):
        scheme = 'http'
    return f'{scheme}://{host}'


def _b64url_nopad(data):
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def hash_secret(secret):
    salt = secrets.token_bytes(16)
    rounds = 200000
    digest = hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), salt, rounds)
    return f'pbkdf2_sha256${rounds}${_b64url_nopad(salt)}${_b64url_nopad(digest)}'


def migrate_plaintext_passwords():
    users = load_json(USERS_FILE, {})
    changed = False
    for _, cfg in users.items():
        plain = str(cfg.get('password') or '')
        if plain:
            cfg['password_hash'] = hash_secret(plain)
            cfg.pop('password', None)
            changed = True
        if cfg.get('password') is not None:
            cfg.pop('password', None)
            changed = True
    if changed:
        save_json(USERS_FILE, users)


def ensure_meta():
    meta = load_json(META_FILE, {})
    changed = False
    if not meta.get('admin_token'):
        meta['admin_token'] = secrets.token_urlsafe(24)
        changed = True
    if not meta.get('admin_user'):
        meta['admin_user'] = 'admin'
        changed = True
    if not meta.get('admin_pass') and not meta.get('admin_pass_hash'):
        meta['admin_pass_hash'] = hash_secret(secrets.token_urlsafe(12))
        changed = True
    if changed:
        save_json(META_FILE, meta)
    return meta


def migrate_admin_password():
    meta = load_json(META_FILE, {})
    plain = str(meta.get('admin_pass') or '')
    if plain:
        meta['admin_pass_hash'] = hash_secret(plain)
        del meta['admin_pass']
        save_json(META_FILE, meta)


def month_key():
    """Billing cycle resets on the 21st. Before the 21st belongs to the previous cycle."""
    now = datetime.now()
    if now.day >= 21:
        return now.strftime('%Y-%m')
    first = now.replace(day=1)
    prev = first - timedelta(days=1)
    return prev.strftime('%Y-%m')


def usage_for_user(username, usage_month=None):
    if usage_month is None:
        usage_month = load_json(USAGE_FILE, {}).get(month_key(), {})
    current = usage_month.get(username, 0)
    if isinstance(current, dict):
        tx = int(current.get('tx', 0))
        rx = int(current.get('rx', 0))
        total = int(current.get('total', tx + rx))
        return tx, rx, total
    total = int(current or 0)
    return 0, total, total


def user_total_quota(user_cfg):
    return int(user_cfg.get('monthly_quota_bytes', 0) or 0)


def build_yaml(username, auth_secret):
    if not TEMPLATE_FILE.exists():
        return ''
    text = TEMPLATE_FILE.read_text(encoding='utf-8')
    text = re.sub(
        r'(?m)^(\s*password:\s*).*$',
        lambda m: f'{m.group(1)}{username}:{auth_secret}',
        text,
        count=1,
    )
    users = load_json(USERS_FILE, {})
    vless_uuid = str((users.get(username) or {}).get('vless_uuid') or '').strip()
    if vless_uuid:
        text = re.sub(
            r'(?m)^(\s*uuid:\s*).*$',
            lambda m: f'{m.group(1)}{vless_uuid}',
            text,
        )
    return text


def fmt_bytes(num):
    n = float(max(0, int(num)))
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    return f"{n:.2f} {units[idx]}"


def pct(used, total):
    if total <= 0:
        return 0.0
    return min(100.0, max(0.0, used * 100.0 / total))


def verify_secret(plain, stored_hash):
    """Verify a plaintext value against a pbkdf2 hash."""
    try:
        _, rounds_s, salt_b64, digest_b64 = stored_hash.split('$')
        rounds = int(rounds_s)
        salt = base64.urlsafe_b64decode(salt_b64 + '==')
        expected = base64.urlsafe_b64decode(digest_b64 + '==')
        candidate = hashlib.pbkdf2_hmac('sha256', plain.encode('utf-8'), salt, rounds)
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# In-memory login failure tracker: {ip: [timestamp, ...]}
_login_failures: dict = {}
_LOGIN_MAX = 3        # max failures
_LOGIN_WINDOW = 3600  # seconds (1 hour)


def _is_rate_limited(ip):
    now = time.time()
    times = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_failures[ip] = times
    return len(times) >= _LOGIN_MAX


def _record_failure(ip):
    _login_failures.setdefault(ip, []).append(time.time())


def _clear_failures(ip):
    _login_failures.pop(ip, None)


def check_user_token(user, token):
    users = load_json(USERS_FILE, {})
    cfg = users.get(user)
    if not cfg:
        return None
    expected = str(cfg.get('sub_token') or '')
    if not token or not hmac.compare_digest(token, expected):
        return None
    return cfg


def parse_cookies(handler):
    raw = handler.headers.get('Cookie', '')
    ck = SimpleCookie()
    try:
        ck.load(raw)
    except Exception:
        return {}
    return {k: v.value for k, v in ck.items()}


def get_sessions():
    sessions = load_json(SESSIONS_FILE, {})
    now = int(time.time())
    alive = {}
    for sid, info in sessions.items():
        exp = int(info.get('exp', 0))
        if exp > now:
            alive[sid] = info
    if alive != sessions:
        save_json(SESSIONS_FILE, alive)
    return alive


def create_session(username='admin'):
    sessions = get_sessions()
    sid = secrets.token_urlsafe(24)
    sessions[sid] = {'user': username, 'exp': int(time.time()) + SESSION_TTL}
    save_json(SESSIONS_FILE, sessions)
    return sid


def delete_session(sid):
    if not sid:
        return
    sessions = get_sessions()
    if sid in sessions:
        del sessions[sid]
        save_json(SESSIONS_FILE, sessions)


def is_logged_in(handler):
    q = parse_qs(urlparse(handler.path).query)
    token = (q.get('token') or [''])[0]
    meta = ensure_meta()
    admin_token = str(meta.get('admin_token') or '')
    if token and hmac.compare_digest(token, admin_token):
        return True
    sid = parse_cookies(handler).get('sid', '')
    sessions = get_sessions()
    return sid in sessions


def html_page(title, body):
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title}</title>'
        f'<link rel="stylesheet" href="/static/style.css">'
        f'</head><body><div class="scanline"></div>{body}'
        f'<script>document.addEventListener("DOMContentLoaded",function(){{'
        f'var f=document.querySelector(".flash,.err");'
        f'if(f)setTimeout(function(){{f.style.transition="opacity 0.6s";f.style.opacity="0";'
        f'setTimeout(function(){{f.remove()}},650);}},3500);}});</script>'
        f'</body></html>'
    )


def flash_text(msg):
    if not msg:
        return ''
    if msg == 'login success':
        return '登录成功'
    if msg.startswith('updated '):
        return f'已更新用户：{msg.split(" ", 1)[1]}'
    if msg.startswith('created '):
        return f'已创建用户：{msg.split(" ", 1)[1]}'
    if msg.startswith('reset usage '):
        return f'已清除用户本月已用流量：{msg.split(" ", 2)[2]}'
    if msg == 'reset usage all':
        return '已清除全部用户本月已用流量'
    if msg.startswith('deleted '):
        return f'已删除用户：{msg.split(" ", 1)[1]}'
    maps = {
        'user not found': '用户不存在',
        'user empty': '用户名不能为空',
        'user_exists_use_reset_token': '用户已存在，请勾选”若用户已存在则重置订阅令牌”后再创建',
    }
    return maps.get(msg, msg)


def render_home(host):
    body = f'''<div class="wrap narrow home-shell">
<div class="nav"><div class="brand">Hysteria Control Plane</div><span class="badge">{html.escape(host)}</span></div>
<section class="hero-panel">
  <div class="eyebrow">SECURE NETWORK CONSOLE</div>
  <h1 class="hero">高速节点控制中心</h1>
  <p class="lede">面向 Hysteria2 / Reality 的统一管理入口，提供节点、订阅与流量状态的集中控制。</p>
  <div class="row" style="margin-top:22px;justify-content:center;"><a class="btn" href="/login">进入管理员登录</a><a class="btn secondary" href="/admin">打开控制台</a></div>
</section>
<div class="grid grid-3" style="margin-top:14px;">
  <div class="card"><div class="k">EXTERNAL PORT</div><div class="v">8082</div><div class="small">独立 Nginx 反代入口</div></div>
  <div class="card"><div class="k">BACKEND</div><div class="v">127.0.0.1:8081</div><div class="small">仅本机监听，更安全</div></div>
  <div class="card"><div class="k">STATUS</div><div class="v">Ready</div><div class="small">控制面板在线</div></div>
</div>
</div>'''
    return html_page('Hysteria Control Plane', body)


def render_login(host, msg=''):
    notice = f'<div class="err">{html.escape(msg)}</div>' if msg else ''
    body = f'''<div class="wrap login-shell">
  <div class="login-card inline-form">
    <div class="login-top"><div class="login-mark"></div><span class="ip-pill">{html.escape(host)}</span></div>
    <div class="header" style="margin:0 0 26px;text-align:left;">
      <div class="eyebrow">ADMIN ACCESS</div>
      <h1 class="login-title">控制台登录</h1>
      <p class="login-subtitle">进入 Hysteria 节点控制平面</p>
    </div>
    <div class="progress-bar"><div class="progress-fill" style="width:58%"></div></div>
    {notice}
    <div class="card-wrapper" style="padding:0;background:transparent;border:0;box-shadow:none;backdrop-filter:none;">
      <form method="post" action="/login" autocomplete="on">
        <div class="form-group"><label>用户名</label><input name="username" required autocomplete="username" placeholder="admin"></div>
        <div class="form-group" style="margin-top:16px;"><label>密码</label><input name="password" type="password" required autocomplete="current-password" placeholder="请输入管理员密码"></div>
        <div class="row" style="margin-top:22px;"><button class="btn" type="submit">登录管理后台</button><a class="btn secondary" href="/">返回首页</a></div>
      </form>
    </div>
  </div>
</div>'''
    return html_page('管理员登录', body)


def render_user_panel(host, base_url, user, token, cfg):
    tx, rx, used = usage_for_user(user)
    total = user_total_quota(cfg)
    remain = max(total - used, 0) if total > 0 else -1
    online = int(load_json(ONLINE_FILE, {}).get(user, 0))
    percent = pct(used, total)
    cls = 'danger' if percent >= 90 else ''
    sub_path = f'/sub/{user}?token={token}'
    panel_path = f'/panel/{user}?token={token}'
    sub_http = f'{base_url}{sub_path}'
    body = f'''<div class="wrap"><div class="nav"><div class="brand">用户面板</div><span class="badge">{html.escape(user)}</span></div>
<div class="grid grid-4"><div class="card"><div class="k">本月已用</div><div class="v big">{fmt_bytes(used)}</div></div><div class="card"><div class="k">总流量</div><div class="v">{fmt_bytes(total)}</div></div><div class="card"><div class="k">剩余流量</div><div class="v">{fmt_bytes(remain)}</div></div><div class="card"><div class="k">在线设备</div><div class="v">{online} / {int(cfg.get('max_devices', 0) or 0)}</div></div></div>
<div class="card" style="margin-top:14px;"><div class="k">流量进度 {percent:.2f}%</div><div class="bar"><div class="fill {cls}" style="width:{percent:.2f}%"></div></div><div class="small" style="margin-top:8px;">上传: {fmt_bytes(tx)} | 下载: {fmt_bytes(rx)}</div></div>
<div class="grid grid-2" style="margin-top:14px;"><div class="card"><div class="k">订阅链接</div><div class="mono" id="sub">{html.escape(sub_http)}</div><div class="row"><button class="btn" onclick="copySub()">复制订阅链接</button><a class="btn secondary" href="{html.escape(sub_path)}">打开订阅</a></div></div><div class="card"><div class="k">当前面板链接</div><div class="mono">{html.escape(base_url)}{html.escape(panel_path)}</div><div class="row"><a class="btn secondary" href="/">返回首页</a></div></div></div>
</div>
<script>
function copySub(){{
  const text = document.getElementById('sub').innerText;
  if (navigator.clipboard && window.isSecureContext) {{
    navigator.clipboard.writeText(text).then(() => {{
      alert('已复制订阅链接');
    }}).catch(() => {{
      fallbackCopy(text);
    }});
    return;
  }}
  fallbackCopy(text);
}}

function fallbackCopy(text) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {{
    const ok = document.execCommand('copy');
    alert(ok ? '已复制订阅链接' : '复制失败，请手动复制');
  }} catch (e) {{
    alert('复制失败，请手动复制');
  }}
  document.body.removeChild(ta);
}}
</script>'''
    return html_page(f'{user} 用户面板', body)


def row_form(user, cfg, online, host, base_url, usage_month=None):
    tx, rx, used = usage_for_user(user, usage_month)
    total = user_total_quota(cfg)
    max_devices = int(cfg.get('max_devices', 0) or 0)
    quota_gb = int(round(total / 1024 / 1024 / 1024)) if total > 0 else 0
    panel = f'{base_url}/panel/{user}?token={cfg.get("sub_token", "")}'
    sub_http = f'{base_url}/sub/{user}?token={cfg.get("sub_token", "")}'
    guest_checked = 'checked' if cfg.get('guest') else ''
    percent = pct(used, total)
    bar_cls = 'danger' if percent >= 90 else ''
    bar_w = f'{percent:.1f}'
    user_esc = html.escape(user)
    return f'''<tr data-user="{user_esc}">
<td>{user_esc}<div class="small">在线：<span data-role="online">{online.get(user, 0)}</span> / {max_devices}</div></td>
<td>
  <span style="font-weight:600;" data-role="used">{fmt_bytes(used)}</span><span class="small"> / {fmt_bytes(total)}</span>
  <div class="mini-bar"><div class="mini-fill {bar_cls}" data-role="bar" style="width:{bar_w}%"></div></div>
  <div class="small" data-role="detail">{percent:.1f}% | ↑{fmt_bytes(tx)} ↓{fmt_bytes(rx)}</div>
</td>
<td>
<details>
<summary>编辑套餐</summary>
<form method="post" action="/admin/update" class="inline-form">
<input type="hidden" name="user" value="{user_esc}">
<label>兼容连接密码（可选）</label><input name="password" type="password" placeholder="留空则不修改">
<label style="margin-top:8px;">设备数上限</label><input name="max_devices" type="number" min="1" value="{max_devices or 2}">
<label style="margin-top:8px;">月流量上限 (GB)</label><input name="quota_gb" type="number" min="1" value="{quota_gb or 150}">
<label class="switch" style="margin-top:8px;"><input type="checkbox" name="guest" {guest_checked}>客人用户</label>
<button class="btn" type="submit">保存</button>
</form>
</details>
<form method="post" action="/admin/reset-usage" class="inline-form" style="margin-top:6px;">
<input type="hidden" name="user" value="{user_esc}">
<button class="btn secondary" type="submit">清除流量</button>
</form>
<form method="post" action="/admin/delete" class="inline-form" style="margin-top:6px;" onsubmit="return confirm('确认删除用户 {user_esc}？此操作不可撤销。')">
<input type="hidden" name="user" value="{user_esc}">
<button class="btn danger" type="submit">删除</button>
</form>
</td>
<td><a href="{html.escape(panel)}">用户面板</a><br><a href="{html.escape(sub_http)}">订阅链接</a></td>
</tr>'''


def render_admin(host, base_url, flash=''):
    users = load_json(USERS_FILE, {})
    online = load_json(ONLINE_FILE, {})
    usage_month = load_json(USAGE_FILE, {}).get(month_key(), {})
    total_used = sum(usage_for_user(u, usage_month)[2] for u in users)
    flash = flash_text(flash)
    alert = f'<div class="flash">{html.escape(flash)}</div>' if flash else ''
    rows = ''.join(row_form(u, cfg, online, host, base_url, usage_month) for u, cfg in users.items())
    online_total = sum(int(v or 0) for v in online.values())
    quota_total = sum(user_total_quota(cfg) for cfg in users.values())
    quota_pct = pct(total_used, quota_total)
    body = f'''<div class="wrap">
  <div class="admin-header">
    <span class="ip-pill">{html.escape(host)}</span>
    <h1 style="margin-top:22px;">HYSTERIA CONTROL</h1>
    <p>REALTIME OPERATIONS · 节点管理后台</p>
    <div class="admin-hero-actions">
      <a class="btn secondary" href="/admin/config">模板配置</a>
      <a class="btn secondary" href="/admin/rules">路由规则</a>
      <a class="btn secondary" href="/admin/logs">清零日志</a>
      <a class="btn secondary" href="/logout">退出</a>
    </div>
  </div>
  
  <div class="progress-bar"><div class="progress-fill" style="width:{quota_pct:.2f}%"></div></div>
  {alert}
  <div class="steps-container stats-strip">
    <div class="step-indicator stat-card"><div class="step-number v big" id="total-used">{fmt_bytes(total_used)}</div><div class="step-label">本月总流量</div><div class="small">所有用户实时累积</div></div>
    <div class="step-indicator stat-card"><div class="step-number v big">{len(users)}</div><div class="step-label">用户数量</div><div class="small">已配置订阅身份</div></div>
    <div class="step-indicator stat-card"><div class="step-number v big">{online_total}</div><div class="step-label">在线设备</div><div class="small">来自在线快照</div></div>
    <div class="step-indicator stat-card"><div class="step-number v big">{month_key()}</div><div class="step-label">统计月份</div><div class="small">每月 21 日重置周期</div></div>
  </div>
  <div class="card-wrapper section-gap">
    <div class="row" style="justify-content:space-between;margin-bottom:14px;">
      <div><div class="eyebrow">GLOBAL QUOTA</div><h2 style="font-size:24px;margin:8px 0 4px;">全局配额使用率</h2><div class="small">{fmt_bytes(total_used)} / {fmt_bytes(quota_total)}</div></div>
      <form method="post" action="/admin/reset-usage-all" onsubmit="return confirm('确认清空全部用户本月已用流量？')"><button class="btn secondary" type="submit">一键清空全部已用流量</button></form>
    </div>
    <div class="bar"><div class="fill" style="width:{quota_pct:.2f}%"></div></div>
  </div>
  <div class="card-wrapper section-gap" style="overflow:auto;">
    <div class="eyebrow">USER TABLE</div>
    <h2 style="font-size:24px;margin:8px 0 14px;">用户列表</h2>
    <table class="table"><thead><tr><th>用户</th><th>用量</th><th>操作</th><th>链接</th></tr></thead><tbody>{rows}</tbody></table>
  </div>
<script>
(function(){{
  function fmt(n){{n=Math.max(0,Number(n)||0);var u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<u.length-1){{n/=1024;i++;}}return n.toFixed(2)+' '+u[i];}}
  async function tick(){{
    try{{
      var r=await fetch('/admin/usage.json',{{credentials:'same-origin',cache:'no-store'}});
      if(!r.ok)return;
      var d=await r.json();
      var tu=document.getElementById('total-used');if(tu)tu.textContent=fmt(d.total_used);
      (d.users||[]).forEach(function(u){{
        var tr=document.querySelector('tr[data-user="'+CSS.escape(u.user)+'"]');if(!tr)return;
        var on=tr.querySelector('[data-role="online"]');if(on)on.textContent=u.online;
        var used=tr.querySelector('[data-role="used"]');if(used)used.textContent=fmt(u.used);
        var bar=tr.querySelector('[data-role="bar"]');if(bar){{bar.style.width=u.percent.toFixed(1)+'%';bar.classList.toggle('danger',u.percent>=90);}}
        var det=tr.querySelector('[data-role="detail"]');if(det)det.textContent=u.percent.toFixed(1)+'% | ↑'+fmt(u.tx)+' ↓'+fmt(u.rx);
      }});
    }}catch(e){{}}
  }}
  setInterval(tick,5000);
}})();
</script>
  <div class="card-wrapper section-gap">
    <details open><summary style="font-size:15px;">新增用户</summary>
      <form method="post" action="/admin/add" class="inline-form" style="margin-top:18px;">
        <div class="grid grid-3"><div><label>用户名</label><input name="user" required placeholder="例如 user01"></div><div><label>兼容连接密码（可选）</label><input name="password" type="password" placeholder="默认仅用订阅 token 认证"></div><div><label>月流量上限 (GB)</label><input name="quota_gb" type="number" value="150" min="1"></div></div>
        <div class="row" style="margin-top:14px;"><label class="switch"><input type="checkbox" name="guest" checked>客人用户</label><label class="switch"><input type="checkbox" name="reset_token">若用户已存在则重置订阅令牌</label></div>
        <button class="btn" style="margin-top:16px;" type="submit">创建用户</button>
      </form>
    </details>
  </div>
</div>'''
    return html_page('管理后台', body)


def _action_label(action):
    return {'reset_usage_user': '清除用户流量', 'reset_usage_all': '清空全部流量'}.get(action, action)


def render_reset_logs(host, limit=300):
    rows = []
    if RESET_LOG_FILE.exists():
        with RESET_LOG_FILE.open('r', encoding='utf-8') as f:
            raw_lines = f.readlines()[-limit:]
        for line in reversed(raw_lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            t = html.escape(str(entry.get('time', '')))
            actor = html.escape(str(entry.get('actor', '')))
            ip = html.escape(str(entry.get('ip', '')))
            action = html.escape(_action_label(str(entry.get('action', ''))))
            target = html.escape(str(entry.get('target', '')))
            month = html.escape(str(entry.get('month', '')))
            before = entry.get('before', {})
            after = entry.get('after', {})
            if isinstance(before, dict) and 'total' in before:
                detail = f'{fmt_bytes(before.get("total", 0))} → {fmt_bytes(after.get("total", 0))}'
            else:
                detail = ''
            rows.append(f'<tr><td class="small">{t}</td><td>{actor}</td><td class="small">{ip}</td>'
                        f'<td>{action}</td><td>{target}</td><td class="small">{month}</td>'
                        f'<td class="small">{html.escape(detail)}</td></tr>')
    table = (''.join(rows)) if rows else '<tr><td colspan="7" style="color:var(--muted)">暂无日志记录</td></tr>'
    body = f'''<div class="wrap center-shell"><div class="nav"><div class="brand">清零日志</div><span class="badge">{html.escape(host)}</span></div>
<div class="card"><div class="k">展示最近 {limit} 条，最新在最上方</div>
<div class="row"><a class="btn secondary" href="/admin">返回管理后台</a></div></div>
<div class="card" style="margin-top:14px; overflow:auto;">
<table class="table"><thead><tr><th>时间</th><th>操作人</th><th>IP</th><th>操作</th><th>目标</th><th>月份</th><th>流量变化</th></tr></thead>
<tbody>{table}</tbody></table></div>
</div>'''
    return html_page('清零日志', body)


def _load_yaml_file(path):
    import yaml
    text = path.read_text(encoding='utf-8')
    return yaml.safe_load(text) or {}


def _dump_yaml(data):
    import yaml
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_template_config():
    """Load the subscription template as a dict. Returns {} if missing."""
    if not TEMPLATE_FILE.exists():
        return {}
    return _load_yaml_file(TEMPLATE_FILE)


def save_template_config(data):
    """Save dict to the subscription template."""
    TEMPLATE_FILE.write_text(_dump_yaml(data), encoding='utf-8')


def render_config_editor(host, flash=''):
    alert = ''
    flash_map = {
        'saved': '模板已保存，所有用户下次拉订阅将使用新配置',
        'err:invalid_json': 'JSON 格式错误，请检查语法',
        'err:empty': '配置内容不能为空',
        'err:load_failed': '加载配置文件失败',
    }
    if flash:
        is_err = flash.startswith('err:')
        msg = flash_map.get(flash, flash)
        cls = 'err' if is_err else 'flash'
        alert = f'<div class="{cls}">{html.escape(msg)}</div>'

    try:
        data = load_template_config()
        config_json = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        config_json = '{}'
        if not flash:
            alert = f'<div class="err">加载配置失败: {html.escape(str(e))}</div>'

    body = f'''<div class="wrap center-shell"><div class="nav"><div class="brand">订阅模板配置</div><span class="badge">{html.escape(host)}</span></div>
{alert}
<div class="card" style="margin-bottom:14px;">
<div class="small" style="margin-bottom:8px;">编辑订阅模板（JSON 格式）。保存后所有用户下次拉订阅即获得新配置，每个用户的密码和 UUID 由服务端从 users.json 自动注入。</div>
<div class="small">模板文件：{html.escape(str(TEMPLATE_FILE))}</div>
</div>
<div class="card">
<form method="post" action="/admin/config/save" id="configForm">
<div style="position:relative;">
<div id="jsonError" style="display:none;color:var(--danger);font-size:13px;margin-bottom:8px;"></div>
<textarea name="config_json" id="configEditor"
  style="width:100%;min-height:min(52vh,520px);padding:12px;border-radius:10px;border:1px solid var(--line);background:#0e1628;color:var(--text);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:13px;line-height:1.5;resize:vertical;tab-size:2;"
  spellcheck="false">{html.escape(config_json)}</textarea>
</div>
<div class="row" style="margin-top:12px;">
<button class="btn" type="submit">保存模板</button>
<button class="btn secondary" type="button" onclick="formatJson()">格式化 JSON</button>
<button class="btn secondary" type="button" onclick="collapseJson()">折叠/展开节点</button>
<a class="btn secondary" href="/admin">返回管理后台</a>
</div>
</form>
</div>
</div>
<script>
var editor = document.getElementById('configEditor');
var errorDiv = document.getElementById('jsonError');

function validateJson() {{
  try {{
    JSON.parse(editor.value);
    errorDiv.style.display = 'none';
    editor.style.borderColor = 'var(--line)';
    return true;
  }} catch(e) {{
    errorDiv.textContent = 'JSON 语法错误: ' + e.message;
    errorDiv.style.display = 'block';
    editor.style.borderColor = 'var(--danger)';
    return false;
  }}
}}

function formatJson() {{
  try {{
    var obj = JSON.parse(editor.value);
    editor.value = JSON.stringify(obj, null, 2);
    errorDiv.style.display = 'none';
    editor.style.borderColor = 'var(--line)';
  }} catch(e) {{
    errorDiv.textContent = 'JSON 语法错误: ' + e.message;
    errorDiv.style.display = 'block';
    editor.style.borderColor = 'var(--danger)';
  }}
}}

function collapseJson() {{
  try {{
    var obj = JSON.parse(editor.value);
    var isCompact = !editor.value.includes('\\n');
    editor.value = isCompact ? JSON.stringify(obj, null, 2) : JSON.stringify(obj);
  }} catch(e) {{}}
}}

// Tab key inserts spaces
editor.addEventListener('keydown', function(e) {{
  if (e.key === 'Tab') {{
    e.preventDefault();
    var start = this.selectionStart;
    var end = this.selectionEnd;
    this.value = this.value.substring(0, start) + '  ' + this.value.substring(end);
    this.selectionStart = this.selectionEnd = start + 2;
  }}
}});

// Live validation on input
var validateTimer;
editor.addEventListener('input', function() {{
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateJson, 500);
}});

// Validate before submit
document.getElementById('configForm').addEventListener('submit', function(e) {{
  if (!validateJson()) {{
    e.preventDefault();
    alert('JSON 格式错误，请修正后再保存');
  }}
}});
</script>'''
    return html_page('订阅模板配置', body)


def load_template_rules():
    """Load rules list from the subscription template."""
    import yaml
    if not TEMPLATE_FILE.exists():
        return []
    text = TEMPLATE_FILE.read_text(encoding='utf-8')
    data = yaml.safe_load(text)
    return data.get('rules', [])


def save_template_rules(rules):
    """Replace the rules section in the subscription template."""
    text = TEMPLATE_FILE.read_text(encoding='utf-8')
    lines = text.split('\n')
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        if start is None and re.match(r'^rules\s*:', line):
            start = i
        elif start is not None and line and not line[0].isspace() and not line.startswith('#'):
            end = i
            break
    new_rule_lines = ['# 6. 规则', 'rules:']
    for r in rules:
        new_rule_lines.append(f"  - '{r}'")
    if start is None:
        result = lines + [''] + new_rule_lines
    else:
        cut = start - 1 if start > 0 and lines[start - 1].startswith('#') else start
        result = lines[:cut] + new_rule_lines + lines[end:]
    TEMPLATE_FILE.write_text('\n'.join(result) + ('\n' if not result[-1].endswith('\n') else ''), encoding='utf-8')


def _parse_clash_rule(rule_str):
    """Parse 'TYPE,value,action[,extra]' into display parts."""
    parts = rule_str.split(',', 2)
    if len(parts) < 2:
        return rule_str, '', '', ''
    rtype = parts[0]
    if rtype == 'MATCH':
        return 'MATCH', '全部', parts[1] if len(parts) > 1 else '', ''
    if len(parts) == 2:
        return rtype, parts[1], '', ''
    # parts[2] may be "action" or "action,no-resolve"
    rest = parts[2].split(',', 1)
    action = rest[0]
    extra = rest[1] if len(rest) > 1 else ''
    return rtype, parts[1], action, extra


_RULE_TYPE_LABELS = {
    'DOMAIN-SUFFIX': '域名后缀', 'DOMAIN-KEYWORD': '域名关键词', 'DOMAIN': '完整域名',
    'IP-CIDR': 'IP 段', 'IP-CIDR6': 'IPv6 段', 'GEOIP': 'GeoIP',
    'RULE-SET': '规则集', 'MATCH': '兜底',
}
_ACTION_LABELS = {'DIRECT': '直连', 'REJECT': '拦截'}


_RULES_FLASH = {
    'rule_added': '规则已添加，客户端更新订阅后生效',
    'rule_deleted': '规则已删除，客户端更新订阅后生效',
    'pattern_empty': '匹配值不能为空',
    'invalid_rule_type': '无效的规则类型',
    'invalid_index': '无效的规则序号',
    'index_out_of_range': '规则序号超出范围',
    'raw_saved': '全部规则已保存，客户端更新订阅后生效',
    'raw_empty': '规则不能为空',
}


def render_rules(host, flash=''):
    rules = load_template_rules()
    alert = ''
    if flash:
        is_err = flash.startswith('err:')
        key = flash.removeprefix('err:')
        msg = _RULES_FLASH.get(key, key)
        cls = 'err' if is_err else 'flash'
        alert = f'<div class="{cls}">{html.escape(msg)}</div>'

    rows = ''
    for i, rule_str in enumerate(rules):
        rtype, val, action, extra = _parse_clash_rule(rule_str)
        type_label = _RULE_TYPE_LABELS.get(rtype, rtype)
        action_label = _ACTION_LABELS.get(action, action)
        extra_tag = f' <span class="small">({html.escape(extra)})</span>' if extra else ''
        is_system = rtype in ('RULE-SET', 'GEOIP', 'MATCH')
        del_btn = ''
        if not is_system:
            del_btn = (
                f'<form method="post" action="/admin/rules/delete" style="display:inline;" '
                f'onsubmit="return confirm(\'确认删除此规则？\')">'
                f'<input type="hidden" name="index" value="{i}">'
                f'<button class="btn danger" style="padding:4px 10px;font-size:12px;" type="submit">删除</button>'
                f'</form>'
            )
        style = ' style="color:var(--muted);"' if is_system else ''
        rows += (
            f'<tr{style}><td>{i + 1}</td><td>{html.escape(type_label)}</td>'
            f'<td style="word-break:break-all;">{html.escape(val)}</td>'
            f'<td>{html.escape(action_label)}{extra_tag}</td>'
            f'<td>{del_btn}</td></tr>'
        )

    rules_text = html.escape('\n'.join(rules))

    body = f'''<div class="wrap center-shell"><div class="nav"><div class="brand">订阅路由规则</div><span class="badge">{len(rules)} 条</span></div>
{alert}
<div class="card" style="overflow:auto;">
<div class="small" style="margin-bottom:10px;">自定义规则优先级高于规则集，从上到下依次匹配。灰色行为内置规则集，不可删除。</div>
<table class="table"><thead><tr><th>#</th><th>类型</th><th>匹配</th><th>动作</th><th>操作</th></tr></thead>
<tbody>{rows}</tbody></table></div>

<div class="card" style="margin-top:14px;">
<div style="font-weight:600;margin-bottom:12px;">添加自定义规则</div>
<form method="post" action="/admin/rules/add" class="inline-form">
<div class="grid grid-2" style="gap:10px;">
<div><label>规则类型</label><select name="rule_type">
<option value="DOMAIN-SUFFIX">DOMAIN-SUFFIX（域名后缀）</option>
<option value="DOMAIN-KEYWORD">DOMAIN-KEYWORD（域名关键词）</option>
<option value="DOMAIN">DOMAIN（完整域名）</option>
<option value="IP-CIDR">IP-CIDR（IP 段）</option>
</select></div>
<div><label>匹配值</label><input name="pattern" required placeholder="example.com 或 10.0.0.0/8"></div>
<div><label>动作</label><select name="action">
<option value="DIRECT">直连 (DIRECT)</option>
<option value="🚀 节点选择">代理 (🚀 节点选择)</option>
<option value="REJECT">拦截 (REJECT)</option>
</select></div>
<div><label>附加选项</label><select name="extra">
<option value="">无</option>
<option value="no-resolve">no-resolve（IP 规则跳过 DNS 解析）</option>
</select></div>
</div>
<div class="row" style="margin-top:10px;">
<button class="btn" type="submit">添加规则（插入到最前）</button>
<a class="btn secondary" href="/admin">返回管理后台</a>
</div>
</form>
</div>

<div class="card" style="margin-top:14px;">
<details>
<summary style="font-size:14px;color:var(--accent-2);cursor:pointer;">直接编辑全部规则</summary>
<form method="post" action="/admin/rules/raw" class="inline-form" style="margin-top:12px;">
<div class="small" style="margin-bottom:8px;">每行一条规则，格式：<code>TYPE,匹配值,动作</code>。保存后同步到所有订阅模板。</div>
<textarea name="rules_raw" style="width:100%;min-height:min(38vh,320px);padding:10px;border-radius:10px;border:1px solid var(--line);background:#0e1628;color:var(--text);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:13px;line-height:1.6;resize:vertical;">{rules_text}</textarea>
<div class="row" style="margin-top:10px;">
<button class="btn" type="submit">保存全部规则</button>
</div>
</form>
</details>
</div>
</div>'''
    return html_page('订阅路由规则', body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def parse_form(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(length).decode('utf-8', errors='ignore')
        return parse_qs(body)

    def send_response_body(self, code, body, ctype='text/plain; charset=utf-8', send_body=True, extra_headers=None):
        data = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if 'text/html' in ctype:
            self.send_header('Cache-Control', 'no-store')
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def redirect(self, to, cookie=None):
        self.send_response(302)
        self.send_header('Location', to)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()

    def get_admin_actor(self):
        q = parse_qs(urlparse(self.path).query)
        token = (q.get('token') or [''])[0]
        meta = ensure_meta()
        admin_token = str(meta.get('admin_token') or '')
        if token and hmac.compare_digest(token, admin_token):
            return 'token-admin'
        sid = parse_cookies(self).get('sid', '')
        sessions = get_sessions()
        if sid in sessions:
            return sessions[sid].get('user', 'admin')
        return 'unknown'

    def write_reset_log(self, actor, action, target, before, after):
        line = {
            'time': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'actor': actor,
            'ip': self.client_address[0] if self.client_address else '',
            'action': action,
            'target': target,
            'month': month_key(),
            'before': before,
            'after': after,
        }
        RESET_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RESET_LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(json.dumps(line, ensure_ascii=True) + '\n')

    def handle_get(self, send_payload=True):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        host = sanitize_host(self.headers.get('Host', '127.0.0.1'))
        base_url = safe_base_url(host, self.headers.get('X-Forwarded-Proto', 'http'))

        if path == '/static/style.css':
            css = BASE_CSS.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/css; charset=utf-8')
            self.send_header('Content-Length', str(len(css)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            if send_payload:
                self.wfile.write(css)
            return

        if path == '/':
            self.send_response_body(200, render_home(host), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/login':
            self.send_response_body(200, render_login(host), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/logout':
            sid = parse_cookies(self).get('sid', '')
            delete_session(sid)
            self.redirect('/login', cookie='sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
            return

        if path.startswith('/sub/'):
            user = path.split('/', 2)[2]
            token = (q.get('token') or [''])[0]
            cfg = check_user_token(user, token)
            if not cfg:
                self.send_response_body(403, '无权限访问', send_body=send_payload)
                return
            yml = build_yaml(user, str(cfg.get('sub_token') or ''))
            tx, rx, used = usage_for_user(user)
            total = user_total_quota(cfg)
            payload = yml.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/yaml; charset=utf-8')
            self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{user}.yaml")
            self.send_header('profile-update-interval', '24')
            self.send_header('subscription-userinfo', f'upload={tx}; download={rx}; total={total}; expire=0')
            self.send_header('x-usage-total-bytes', str(used))
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if send_payload:
                self.wfile.write(payload)
            return

        if path.startswith('/panel/'):
            user = path.split('/', 2)[2]
            token = (q.get('token') or [''])[0]
            cfg = check_user_token(user, token)
            if not cfg:
                self.send_response_body(403, '无权限访问', send_body=send_payload)
                return
            self.send_response_body(200, render_user_panel(host, base_url, user, token, cfg), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/admin':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            flash = (q.get('msg') or [''])[0]
            self.send_response_body(200, render_admin(host, base_url, flash=flash), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/admin/usage.json':
            if not is_logged_in(self):
                self.send_response_body(403, '{"error":"unauthorized"}', 'application/json; charset=utf-8', send_payload)
                return
            users = load_json(USERS_FILE, {})
            online = load_json(ONLINE_FILE, {})
            usage_month = load_json(USAGE_FILE, {}).get(month_key(), {})
            user_list = []
            total_used = 0
            for u, cfg in users.items():
                tx, rx, used = usage_for_user(u, usage_month)
                total = user_total_quota(cfg)
                total_used += used
                user_list.append({
                    'user': u,
                    'tx': tx,
                    'rx': rx,
                    'used': used,
                    'total': total,
                    'percent': pct(used, total),
                    'online': int(online.get(u, 0)),
                })
            payload = json.dumps({'total_used': total_used, 'users': user_list}, ensure_ascii=True)
            self.send_response_body(200, payload, 'application/json; charset=utf-8', send_payload)
            return

        if path == '/admin/logs':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            self.send_response_body(200, render_reset_logs(host), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/admin/config':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            flash = (q.get('msg') or [''])[0]
            self.send_response_body(200, render_config_editor(host, flash=flash), 'text/html; charset=utf-8', send_payload)
            return

        if path == '/admin/rules':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            flash = (q.get('msg') or [''])[0]
            self.send_response_body(200, render_rules(host, flash=flash), 'text/html; charset=utf-8', send_payload)
            return

        self.send_response_body(404, '页面不存在', send_body=send_payload)

    def do_GET(self):
        self.handle_get(send_payload=True)

    def do_HEAD(self):
        self.handle_get(send_payload=False)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        form = self.parse_form()
        meta = ensure_meta()

        if path == '/login':
            ip = self.client_address[0] if self.client_address else ''
            host = sanitize_host(self.headers.get('Host', '127.0.0.1'))
            if _is_rate_limited(ip):
                self.send_response_body(200, render_login(host, msg='登录尝试过于频繁，请 1 小时后再试'), 'text/html; charset=utf-8', True)
                return
            user = (form.get('username') or [''])[0].strip()
            passwd = (form.get('password') or [''])[0]
            stored_hash = str(meta.get('admin_pass_hash') or '')
            ok = (user == meta.get('admin_user') and stored_hash and verify_secret(passwd, stored_hash))
            if ok:
                _clear_failures(ip)
                sid = create_session('admin')
                self.redirect('/admin?msg=login+success', cookie=f'sid={sid}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax')
                return
            _record_failure(ip)
            self.send_response_body(200, render_login(host, msg='用户名或密码错误'), 'text/html; charset=utf-8', True)
            return

        if path == '/admin/update':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            username = (form.get('user') or [''])[0].strip()
            users = load_json(USERS_FILE, {})
            if username not in users:
                self.redirect('/admin?msg=user+not+found')
                return
            cfg = users[username]
            new_password = (form.get('password') or [''])[0].strip()
            max_devices = parse_int_field((form.get('max_devices') or ['2'])[0], 2, 1, 100)
            quota_gb = parse_int_field((form.get('quota_gb') or ['150'])[0], 150, 1, 10240)
            guest = 'guest' in form
            if new_password:
                cfg['password_hash'] = hash_secret(new_password)
            cfg.pop('password', None)
            cfg['max_devices'] = max(1, max_devices)
            cfg['monthly_quota_bytes'] = max(1, quota_gb) * 1024 * 1024 * 1024
            cfg['guest'] = guest
            if not cfg.get('sub_token'):
                cfg['sub_token'] = secrets.token_urlsafe(18)
            users[username] = cfg
            save_json(USERS_FILE, users)
            self.redirect('/admin?msg=updated+' + username)
            return

        if path == '/admin/add':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            username = (form.get('user') or [''])[0].strip()
            password = (form.get('password') or [''])[0].strip()
            quota_gb = parse_int_field((form.get('quota_gb') or ['150'])[0], 150, 1, 10240)
            guest = 'guest' in form
            reset_token = 'reset_token' in form
            users = load_json(USERS_FILE, {})
            if not username:
                self.redirect('/admin?msg=user+empty')
                return
            if username in users and not reset_token:
                self.redirect('/admin?msg=user_exists_use_reset_token')
                return
            existing = users.get(username, {})
            existing_token = existing.get('sub_token')
            token = secrets.token_urlsafe(18) if (reset_token or not existing_token) else existing_token
            vless_uuid = str(existing.get('vless_uuid') or '').strip() or str(uuid.uuid4())
            entry = {
                'guest': guest,
                'max_devices': 2,
                'monthly_quota_bytes': max(1, quota_gb) * 1024 * 1024 * 1024,
                'sub_token': token,
                'vless_uuid': vless_uuid,
            }
            if password:
                entry['password_hash'] = hash_secret(password)
            elif existing.get('password_hash'):
                entry['password_hash'] = existing.get('password_hash')
            users[username] = entry
            save_json(USERS_FILE, users)
            if xray_sync_user(username, vless_uuid):
                xray_reload_async()
            self.redirect('/admin?msg=created+' + username)
            return

        if path == '/admin/reset-usage':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            username = (form.get('user') or [''])[0].strip()
            users = load_json(USERS_FILE, {})
            if username not in users:
                self.redirect('/admin?msg=user+not+found')
                return
            with usage_lock():
                usage = load_json(USAGE_FILE, {})
                mk = month_key()
                usage.setdefault(mk, {})
                tx, rx, total = usage_for_user(username, usage[mk])
                before = {'tx': tx, 'rx': rx, 'total': total}
                usage[mk][username] = {'tx': 0, 'rx': 0, 'total': 0}
                after = {'tx': 0, 'rx': 0, 'total': 0}
                save_json(USAGE_FILE, usage)
            self.write_reset_log(self.get_admin_actor(), 'reset_usage_user', username, before, after)
            self.redirect('/admin?msg=reset+usage+' + username)
            return

        if path == '/admin/reset-usage-all':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            with usage_lock():
                usage = load_json(USAGE_FILE, {})
                mk = month_key()
                usage.setdefault(mk, {})
                before_all = {}
                users = load_json(USERS_FILE, {})
                for username in users.keys():
                    tx, rx, total = usage_for_user(username, usage[mk])
                    before_all[username] = {'tx': tx, 'rx': rx, 'total': total}
                    usage[mk][username] = {'tx': 0, 'rx': 0, 'total': 0}
                save_json(USAGE_FILE, usage)
            self.write_reset_log(
                self.get_admin_actor(),
                'reset_usage_all',
                'all_users',
                before_all,
                {u: {'tx': 0, 'rx': 0, 'total': 0} for u in users.keys()},
            )
            self.redirect('/admin?msg=reset+usage+all')
            return

        if path == '/admin/delete':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            username = (form.get('user') or [''])[0].strip()
            users = load_json(USERS_FILE, {})
            if username not in users:
                self.redirect('/admin?msg=user+not+found')
                return
            del users[username]
            save_json(USERS_FILE, users)
            hy_kick([username])
            if xray_remove_user(username):
                xray_reload_async()
            self.redirect('/admin?msg=deleted+' + username)
            return

        if path == '/admin/config/save':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            raw = (form.get('config_json') or [''])[0].strip()
            if not raw:
                self.redirect('/admin/config?msg=err:empty')
                return
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                self.redirect('/admin/config?msg=err:invalid_json')
                return
            save_template_config(data)
            self.redirect('/admin/config?msg=saved')
            return

        if path == '/admin/rules/add':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            rule_type = (form.get('rule_type') or ['DOMAIN-SUFFIX'])[0]
            pattern = (form.get('pattern') or [''])[0].strip()
            action = (form.get('action') or ['DIRECT'])[0]
            extra = (form.get('extra') or [''])[0]
            if not pattern:
                self.redirect('/admin/rules?msg=err:pattern_empty')
                return
            if rule_type not in ('DOMAIN-SUFFIX', 'DOMAIN-KEYWORD', 'DOMAIN', 'IP-CIDR'):
                self.redirect('/admin/rules?msg=err:invalid_rule_type')
                return
            rule_str = f'{rule_type},{pattern},{action}'
            if extra:
                rule_str += f',{extra}'
            rules = load_template_rules()
            rules.insert(0, rule_str)
            save_template_rules(rules)
            self.redirect('/admin/rules?msg=rule_added')
            return

        if path == '/admin/rules/delete':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            try:
                idx = int((form.get('index') or [''])[0])
            except (ValueError, IndexError):
                self.redirect('/admin/rules?msg=err:invalid_index')
                return
            rules = load_template_rules()
            if idx < 0 or idx >= len(rules):
                self.redirect('/admin/rules?msg=err:index_out_of_range')
                return
            rules.pop(idx)
            save_template_rules(rules)
            self.redirect('/admin/rules?msg=rule_deleted')
            return

        if path == '/admin/rules/raw':
            if not is_logged_in(self):
                self.redirect('/login')
                return
            raw = (form.get('rules_raw') or [''])[0]
            rules = [line.strip() for line in raw.splitlines() if line.strip()]
            if not rules:
                self.redirect('/admin/rules?msg=err:raw_empty')
                return
            save_template_rules(rules)
            self.redirect('/admin/rules?msg=raw_saved')
            return

        self.send_response_body(404, '页面不存在')


if __name__ == '__main__':
    ensure_meta()
    migrate_plaintext_passwords()
    migrate_admin_password()
    srv = ThreadingHTTPServer(LISTEN, Handler)
    srv.serve_forever()
