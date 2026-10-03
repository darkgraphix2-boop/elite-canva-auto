"""
ELITE SLOTS - Surfshark slot onboarding backend
-----------------------------------------------
Client apne Surfshark app ka 6-letter login code submit karta hai,
owner ko instant Telegram notification jati hai, owner apne
(logged-in) Surfshark app mein wo code daal kar client ko approve
kar deta hai. Sab kuch dashboard pe track hota hai.

Chalane ke liye:  start.bat   (ya:  python -m uvicorn main:app --port 8000)
Admin dashboard:  http://127.0.0.1:8000/admin
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ELITE_DATA_DIR") or BASE)
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "slots.db"
NOTIFY_LOG = DATA_DIR / "notify.log"

CODE_RE = re.compile(r"^[A-Z0-9]{6}$")
CODE_TTL = 5 * 60 + 30  # Surfshark code ~5 min baad refresh hota hai

app = FastAPI(title="Elite Slots")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            (BASE / "config.example.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = load_config()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                   id TEXT PRIMARY KEY,
                   code TEXT NOT NULL,
                   buyer TEXT DEFAULT '',
                   ip TEXT DEFAULT '',
                   created_at REAL NOT NULL,
                   status TEXT DEFAULT 'pending',
                   updated_at REAL DEFAULT 0
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS canva_jobs (
                   id TEXT PRIMARY KEY,
                   email TEXT NOT NULL,
                   buyer TEXT DEFAULT '',
                   chat_id TEXT DEFAULT '',
                   source TEXT DEFAULT 'telegram',
                   created_at REAL NOT NULL,
                   status TEXT DEFAULT 'pending',
                   note TEXT DEFAULT '',
                   updated_at REAL DEFAULT 0
               )"""
        )


init_db()


async def notify_owner(text: str):
    token = CONFIG.get("bot_token", "")
    chat_id = CONFIG.get("owner_chat_id", "")
    tg_base = (CONFIG.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
    if token and "PASTE" not in token and chat_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{tg_base}/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
        except Exception as exc:
            print("[notify] Telegram error:", exc)
    else:
        print("[notify] config.json mein bot_token/owner_chat_id nahi hai")
    with NOTIFY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + " | " + text.replace("\n", " / ") + "\n")


# ---------------------------------------------------------------- auto approve

API_RESULT_MSG = {
    "approved": "✅ AUTO-APPROVE ho gaya! (cloud/API)\nCode: {code}\nClient ka device login ho gaya.",
    "invalid": "⚠️ Code ghalat ya expired tha\nCode: {code}\nClient se naya code mangwao.",
    "no_session": "🚫 Backend Surfshark mein login NAHI hai!\nCode: {code}\n/login page pe email+password se login karo.",
    "network_error": "🌐 Surfshark API tak nahi pahunch sade (network)\nCode: {code}\nShayad VPN/server ka masla - thori der baad dobara.",
}


def _notify_from_thread(text: str):
    """notify_owner (async) ko worker thread se call karne ke liye bridge."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(notify_owner(text), loop)
    else:
        asyncio.run(notify_owner(text))


def _auto_approve_job(code: str, rid: str):
    mode = CONFIG.get("approve_mode", "api")

    if mode == "api":
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import surfshark_api
            result = surfshark_api.approve(code)
        except Exception as exc:
            result = f"error:{exc}"
        new_status = "approved" if result == "approved" else ("rejected" if result == "invalid" else None)
    else:
        # "ui" mode: Windows app automation (sirf apne PC pe kaam karta hai)
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            import auto_approve
            result = auto_approve.approve_code(code)
        except Exception as exc:
            result = f"error:{exc}"
        new_status = "approved" if result == "approved" else None

    if new_status:
        with db() as conn:
            conn.execute(
                "UPDATE requests SET status=?, updated_at=? WHERE id=?",
                (new_status, time.time(), rid),
            )

    msg = API_RESULT_MSG.get(result) if mode == "api" else AUTO_RESULT_MSG_UI.get(result)
    if msg:
        _notify_from_thread(msg.format(code=code))
    else:
        _notify_from_thread(f"❌ AUTO-APPROVE error\nCode: {code}\n{result}")

    print(f"[auto-approve:{mode}] {code} -> {result}")


AUTO_RESULT_MSG_UI = {
    "approved": "✅ AUTO-APPROVE ho gaya! (PC/app automation)\nCode: {code}",
    "invalid_code": "⚠️ Code ghalat/expired tha\nCode: {code}",
    "no_app": "🚫 Surfshark app band hai - UI approve nahi ho saka\nCode: {code}",
    "unknown": "❓ Result confirm nahi hua\nCode: {code}",
}


# ---------------------------------------------------------------- owner surfshark login page

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Surfshark Login - {SHOP}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f1420; color: #e8ecf4;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card { background: #171e2e; border: 1px solid #26304a; border-radius: 16px;
          max-width: 420px; width: 100%; padding: 28px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #8b96ad; font-size: 13px; margin-bottom: 18px; line-height: 1.5; }
  label { display: block; font-size: 13px; color: #8b96ad; margin: 12px 0 6px; }
  input { width: 100%; background: #0f1420; border: 1px solid #2c3a5c; color: #fff;
          border-radius: 10px; padding: 12px 14px; font-size: 15px; }
  input:focus { outline: none; border-color: #2dd4bf; }
  button { width: 100%; margin-top: 18px; background: #2dd4bf; border: none; color: #06281f;
           font-size: 15px; font-weight: 700; padding: 12px; border-radius: 10px; cursor: pointer; }
  .msg { margin-top: 14px; font-size: 14px; line-height: 1.5; display: none; padding: 12px; border-radius: 10px; }
  .ok { display: block; background: #123a33; color: #6ee7b7; }
  .err { display: block; background: #3a1a1a; color: #fca5a5; }
  .warn { display: block; background: #3a3117; color: #fcd34d; }
  .status { padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 6px; }
  .on { background: #123a33; color: #6ee7b7; }
  .off { background: #3a1a1a; color: #fca5a5; }
</style></head><body>
<div class="card">
  <h1>Surfshark Backend Login</h1>
  <div class="sub">Telegram bot jaisa - ek dafa login karo, session save ho jayegi.
  Password kahin save nahi hota, sirf token (renew hota rehta hai).</div>
  <div id="sessbox"></div>
  <form id="f">
    <label>Surfshark email</label>
    <input id="email" type="email" autocomplete="off" placeholder="aap@email.com">
    <label>Password</label>
    <input id="pass" type="password" autocomplete="off" placeholder="password">
    <div id="otprow" style="display:none">
      <label>OTP (2FA code - email/authenticator se)</label>
      <input id="otp" maxlength="8" autocomplete="off" placeholder="123456">
    </div>
    <button id="btn" type="submit">Login</button>
    <div id="msg" class="msg"></div>
  </form>
</div>
<script>
async function loadSess() {
  const r = await fetch('/api/session_status'); const j = await r.json();
  const b = document.getElementById('sessbox');
  if (j.logged_in) {
    b.innerHTML = '<div class="status on">🟢 Logged in: ' + (j.email||'?') +
      (j.expires ? ' | renew: ' + j.expires : '') + '</div>';
  } else {
    b.innerHTML = '<div class="status off">🔴 Surfshark login nahi hai - neeche login karo</div>';
  }
}
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = document.getElementById('msg'), btn = document.getElementById('btn');
  msg.className = 'msg'; btn.disabled = true;
  const body = { email: document.getElementById('email').value.trim(),
                 password: document.getElementById('pass').value };
  const otp = document.getElementById('otp').value.trim();
  const url = otp ? '/api/otp' : '/api/login';
  if (otp) body.otp = otp;
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (j.status === 'ok') { msg.textContent = j.message; msg.className = 'msg ok'; loadSess(); }
    else if (j.status === 'need_otp') { msg.textContent = j.message; msg.className = 'msg warn';
      document.getElementById('otprow').style.display = 'block'; }
    else { msg.textContent = j.message || 'Login fail'; msg.className = 'msg err'; }
  } catch { msg.textContent = 'Server error'; msg.className = 'msg err'; }
  btn.disabled = false;
});
loadSess();
</script>
</body></html>"""


# ---------------------------------------------------------------- buyer page

BUYER_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SHOP} - Surfshark Access</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f1420; color: #e8ecf4;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card { background: #171e2e; border: 1px solid #26304a; border-radius: 16px;
          max-width: 460px; width: 100%; padding: 28px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #8b96ad; font-size: 14px; margin-bottom: 20px; }
  ol { margin: 0 0 20px 20px; color: #c3cbdc; font-size: 14px; line-height: 1.7; }
  label { display: block; font-size: 13px; color: #8b96ad; margin: 12px 0 6px; }
  input { width: 100%; background: #0f1420; border: 1px solid #2c3a5c; color: #fff;
          border-radius: 10px; padding: 12px 14px; font-size: 18px; letter-spacing: 4px;
          text-transform: uppercase; text-align: center; }
  input:focus { outline: none; border-color: #2dd4bf; }
  .small input { font-size: 15px; letter-spacing: 0; text-transform: none; text-align: left; }
  button { width: 100%; margin-top: 18px; background: #2dd4bf; border: none; color: #06281f;
           font-size: 16px; font-weight: 700; padding: 13px; border-radius: 10px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .msg { margin-top: 14px; font-size: 14px; line-height: 1.5; display: none;
         padding: 12px; border-radius: 10px; }
  .ok  { display: block; background: #123a33; color: #6ee7b7; }
  .err { display: block; background: #3a1a1a; color: #fca5a5; }
  .note { margin-top: 16px; font-size: 12px; color: #8b96ad; line-height: 1.5; }
</style></head><body>
<div class="card">
  <h1>{SHOP}</h1>
  <div class="sub">Surfshark access - login code se</div>
  <ol>
    <li>Apne device pe Surfshark app kholo aur login screen pe <b>"Use another device"</b> / QR wala option choose karo.</li>
    <li>Wahan ek <b>6-letter login code</b> dikhega (jaise 9V6A3T).</li>
    <li>Wohi code neeche submit karo - hum foran approve kar denge aur app khud login ho jayega.</li>
  </ol>
  <form id="f">
    <label>Login code (6 letters)</label>
    <input id="code" maxlength="6" autocomplete="off" placeholder="9V6A3T" required>
    <div class="small">
      <label>Apna Telegram username (optional)</label>
      <input id="buyer" maxlength="60" autocomplete="off" placeholder="@username">
    </div>
    <button id="btn" type="submit">Submit code</button>
    <div id="msg" class="msg"></div>
  </form>
  <div class="note">Code har 5 minute baad refresh hota hai - submit karne ke baad apni
  Surfshark screen band na karo jab tak access na mil jaye.</div>
</div>
<script>
const f = document.getElementById('f'), msg = document.getElementById('msg'),
      btn = document.getElementById('btn'), code = document.getElementById('code');
code.addEventListener('input', () => code.value = code.value.toUpperCase().replace(/[^A-Z0-9]/g,''));
f.addEventListener('submit', async (e) => {
  e.preventDefault(); msg.className = 'msg'; btn.disabled = true;
  try {
    const r = await fetch('/api/claim', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: code.value, buyer: document.getElementById('buyer').value})
    });
    const j = await r.json();
    if (j.ok) { msg.textContent = 'Code mil gaya! Owner ko bata diya gaya hai - 1-2 minute wait karo, apni Surfshark app pe bhi screen pe rehna.'; msg.className = 'msg ok'; }
    else { msg.textContent = j.error || 'Kuch masla hua, dobara koshish karo.'; msg.className = 'msg err'; }
  } catch { msg.textContent = 'Server se rabta nahi hua, dobara koshish karo.'; msg.className = 'msg err'; }
  btn.disabled = false;
});
</script>
</body></html>"""


# ---------------------------------------------------------------- admin page

ADMIN_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Admin - {SHOP}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f1420; color: #e8ecf4; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 16px; }
  input { background: #0f1420; border: 1px solid #2c3a5c; color: #fff; border-radius: 8px;
          padding: 10px 12px; font-size: 15px; }
  button { background: #2dd4bf; border: none; color: #06281f; font-weight: 700;
           padding: 10px 16px; border-radius: 8px; cursor: pointer; }
  .row button { padding: 6px 10px; font-size: 12px; margin-right: 4px; }
  .ghost { background: #26304a; color: #c3cbdc; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #26304a; }
  th { color: #8b96ad; font-weight: 600; font-size: 12px; text-transform: uppercase; }
  .code { font-family: monospace; font-size: 18px; letter-spacing: 3px; font-weight: 700; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .pending  { background: #3a3117; color: #fcd34d; }
  .approved { background: #123a33; color: #6ee7b7; }
  .rejected { background: #3a1a1a; color: #fca5a5; }
  .expired  { background: #22293a; color: #8b96ad; }
  #login { display: none; max-width: 320px; }
  #panel { display: none; }
  .hint { color: #8b96ad; font-size: 12px; margin-top: 8px; }
</style></head><body>
<div id="login">
  <h1>Admin login</h1>
  <input id="pass" type="password" placeholder="Passphrase" style="width:200px">
  <button onclick="login()">Login</button>
  <div class="hint">Passphrase config.json ke "admin_pass" mein hai.</div>
</div>
<div id="panel">
  <h1>{SHOP} - slot requests <button class="ghost" style="float:right" onclick="logout()">Logout</button></h1>
  <div id="surfbar" style="margin-bottom:12px; font-size:14px"></div>
  <table><thead><tr>
    <th>Waqt</th><th>Code</th><th>Buyer</th><th>Status</th><th>Code valid</th><th></th>
  </tr></thead><tbody id="rows"></tbody></table>
  <div class="hint">Page khud 5 second baad refresh hota hai. "Code valid" = kitna time
  bacha hai code ke refresh hone se (Surfshark code ~5 min ka hota hai).</div>
</div>
<script>
async function login() {
  const r = await fetch('/admin/login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pass: document.getElementById('pass').value})});
  if ((await r.json()).ok) { location.reload(); } else { alert('Ghalat passphrase'); }
}
function logout() { document.cookie = 'admin=; Max-Age=0; path=/'; location.reload(); }
async function setStatus(id, status) {
  await fetch('/admin/status', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id, status})});
  load();
}
function fmtLeft(created) {
  const left = Math.round((created + 330 - Date.now()/1000));
  if (left <= 0) return '0:00';
  return Math.floor(left/60) + ':' + String(left%60).padStart(2,'0');
}
async function load() {
  const r = await fetch('/api/requests');
  if (r.status === 401) { loginBox(); return; }
  const rows = (await r.json()).requests;
  document.getElementById('rows').innerHTML = rows.map(x => `
    <tr>
      <td>${new Date(x.created_at*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</td>
      <td class="code">${x.code}</td>
      <td>${x.buyer || '-'}</td>
      <td><span class="badge ${x.status}">${x.status}</span></td>
      <td>${x.status === 'pending' ? fmtLeft(x.created_at) : '-'}</td>
      <td class="row">
        <button onclick="setStatus('${x.id}','approved')">Approve</button>
        <button class="ghost" onclick="setStatus('${x.id}','rejected')">Reject</button>
      </td>
    </tr>`).join('') || '<tr><td colspan="6" style="color:#8b96ad">Abhi koi request nahi aayi.</td></tr>';
}
async function loadSurf() {
  try {
    const r = await fetch('/api/session_status');
    if (r.status === 401) return;
    const j = await r.json();
    const b = document.getElementById('surfbar');
    if (j.logged_in) b.innerHTML = '🟢 Surfshark: <b>logged in</b> (' + (j.email||'?') + ') &nbsp; <a href="/login" style="color:#2dd4bf">manage</a>';
    else b.innerHTML = '🔴 Surfshark: <b>login nahi hai</b> - &nbsp;<a href="/login" style="color:#2dd4bf">abhi login karo</a>&nbsp; (warna codes auto approve nahi honge)';
  } catch {}
}
function loginBox() { document.getElementById('login').style.display = 'block'; document.getElementById('panel').style.display = 'none'; }
function panel()    { document.getElementById('login').style.display = 'none'; document.getElementById('panel').style.display = 'block'; }
document.getElementById('pass').addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
(async () => {
  const r = await fetch('/api/requests');
  (r.status === 401) ? loginBox() : panel();
  if (r.status !== 401) { load(); loadSurf(); }
  setInterval(() => { if (document.getElementById('panel').style.display === 'block') load(); }, 5000);
})();
</script>
</body></html>"""


# ---------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def index():
    return BUYER_PAGE.replace("{SHOP}", CONFIG.get("shop_name", "ELITE STORE"))


@app.post("/api/claim")
async def claim(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Ghalat request."}, status_code=400)

    code = str(data.get("code", "")).strip().upper().replace(" ", "")
    buyer = str(data.get("buyer", "")).strip()[:60]
    if not CODE_RE.match(code):
        return JSONResponse(
            {"ok": False, "error": "Code 6 letters/digits ka hota hai (jaise 9V6A3T). Dobara check karo."},
            status_code=400,
        )

    ip = request.client.host if request.client else "?"
    with db() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM requests WHERE ip=? AND status='pending'", (ip,)
        ).fetchone()["c"]
        if pending >= 3:
            return JSONResponse(
                {"ok": False, "error": "Aapki pehle se requests pending hain. Thora intezar karo."},
                status_code=429,
            )
        rid = uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO requests (id, code, buyer, ip, created_at) VALUES (?,?,?,?,?)",
            (rid, code, buyer, ip, time.time()),
        )

    await notify_owner(
        "🔐 NEW SLOT REQUEST\n"
        f"Code: {code}\n"
        f"Buyer: {buyer or 'unknown'}\n\n"
        + ("🤖 AUTO-APPROVE ki koshish jaari hai..." if CONFIG.get("auto_approve", True) else
           "Ye code ~5 min mein refresh hoga - ABHI Surfshark app mein\n"
           "Settings > My Account > Enter login code mein daal do.")
    )
    if CONFIG.get("auto_approve", True):
        threading.Thread(target=_auto_approve_job, args=(code, rid), daemon=True).start()
    return {"ok": True, "id": rid}


def admin_ok(request: Request) -> bool:
    return bool(CONFIG.get("admin_pass")) and request.cookies.get("admin") == CONFIG["admin_pass"]


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    return ADMIN_PAGE.replace("{SHOP}", CONFIG.get("shop_name", "ELITE STORE"))


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not admin_ok(request):
        return HTMLResponse("<meta http-equiv='refresh' content='0;url=/admin'>", status_code=401)
    return LOGIN_PAGE.replace("{SHOP}", CONFIG.get("shop_name", "ELITE STORE"))


@app.post("/api/login")
async def api_login(request: Request):
    if not admin_ok(request):
        return JSONResponse({"status": "fail", "message": "Admin login karo"}, status_code=401)
    data = await request.json()
    import surfshark_api
    status, message = surfshark_api.login(str(data.get("email", "")).strip(),
                                          str(data.get("password", "")))
    return {"status": status, "message": message}


@app.post("/api/otp")
async def api_otp(request: Request):
    if not admin_ok(request):
        return JSONResponse({"status": "fail", "message": "Admin login karo"}, status_code=401)
    data = await request.json()
    import surfshark_api
    status, message = surfshark_api.verify_otp(str(data.get("otp", "")))
    return {"status": status, "message": message}


@app.get("/api/session_status")
async def session_status(request: Request):
    if not admin_ok(request):
        return JSONResponse({"logged_in": False}, status_code=401)
    import surfshark_api
    if not surfshark_api.SESSION.logged_in:
        return {"logged_in": False}
    info = surfshark_api.account_info()
    if info is None:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "email": surfshark_api.SESSION.email or info.get("email"),
        "expires": (info.get("subscription") or {}).get("expiresAt", "") if isinstance(info.get("subscription"), dict) else "",
    }


# ---------------------------------------------------------------- canva login (Surfshark /login jaisa)

CANVA_PAGE = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>Canva Login - {SHOP}</title>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
 body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;display:flex;
      justify-content:center;padding:40px 16px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px;
       max-width:420px;width:100%}
 h2{margin:0 0 6px} .sub{color:#8b949e;font-size:13px;margin-bottom:18px}
 input{width:100%;box-sizing:border-box;background:#0d1117;border:1px solid #30363d;
       color:#e6edf3;border-radius:8px;padding:11px;margin:7px 0;font-size:14px}
 button{width:100%;background:#238636;color:#fff;border:0;border-radius:8px;
        padding:11px;font-size:14px;font-weight:600;cursor:pointer;margin-top:10px}
 button:disabled{opacity:.6}
 .badge{padding:5px 12px;border-radius:999px;font-size:13px;display:inline-block;margin-bottom:14px}
 .on{background:#12331f;color:#3fb950} .off{background:#3d1d1d;color:#f85149}
 #msg{margin-top:12px;font-size:13px;min-height:18px;color:#8b949e;white-space:pre-wrap}
 .hide{display:none}
</style></head><body><div class='card'>
<h2>Canva Login</h2>
<div class='sub'>Canva session cloud pe save hogi - invites khud-ba-khud jayengi</div>
<div id='stat'></div>
<form id='loginForm'>
 <input id='email' type='email' placeholder='Canva email' required>
 <button id='lbtn' type='submit'>Continue — code bhejo</button>
</form>
<form id='otpForm' class='hide'>
 <input id='otp' type='text' placeholder='6-digit code (email mein aya)' required>
 <button id='obtn' type='submit'>Verify code</button>
</form>
<div id='msg'></div>
</div>
<script>
const $=id=>document.getElementById(id);
async function status(){
 try{const r=await fetch('/api/canva/status');const j=await r.json();
  $('stat').innerHTML=j.logged_in
    ? `<span class='badge on'>🟢 Canva login: ${j.email||'?'}</span>`
    : `<span class='badge off'>🔴 Canva login nahi hai</span>`;
 }catch(e){$('stat').innerHTML=`<span class='badge off'>🔴 status nahi mila</span>`;}
}
$('loginForm').onsubmit=async e=>{e.preventDefault();
 $('lbtn').disabled=true;$('msg').textContent='Canva khol raha hoon... (30-60s)';
 try{const r=await fetch('/api/canva/login',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({email:$('email').value})});
  const j=await r.json();$('msg').textContent=j.message||j.status;
  if(j.status==='need_otp'){$('loginForm').classList.add('hide');$('otpForm').classList.remove('hide');}
  if(j.status==='ok')status();
 }catch(err){$('msg').textContent='Error: '+err;}
 $('lbtn').disabled=false;};
$('otpForm').onsubmit=async e=>{e.preventDefault();
 $('obtn').disabled=true;$('msg').textContent='Verify ho raha hai...';
 try{const r=await fetch('/api/canva/otp',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({otp:$('otp').value})});
  const j=await r.json();$('msg').textContent=j.message||j.status;
  if(j.status==='ok'){$('otpForm').classList.add('hide');$('loginForm').reset();status();}
  else{$('otpForm').classList.remove('hide');}
 }catch(err){$('msg').textContent='Error: '+err;}
 $('obtn').disabled=false;};
status();
</script></body></html>"""


@app.get("/canva", response_class=HTMLResponse)
def canva_page(request: Request):
    if not admin_ok(request):
        return HTMLResponse("<meta http-equiv='refresh' content='0;url=/admin'>", status_code=401)
    return HTMLResponse(CANVA_PAGE.replace("{SHOP}", CONFIG.get("shop_name", "ELITE STORE")))


@app.get("/api/canva/status")
async def canva_status(request: Request):
    if not admin_ok(request):
        return JSONResponse({"logged_in": False}, status_code=401)
    sess = DATA_DIR / "canva_session.json"
    email = ""
    try:
        email = (DATA_DIR / "canva_email.txt").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return {"logged_in": sess.exists(), "email": email}


@app.post("/api/canva/login")
async def canva_login(request: Request):
    if not admin_ok(request):
        return JSONResponse({"status": "fail", "message": "Admin login karo"}, status_code=401)
    data = await request.json()
    import canva_worker
    res = await asyncio.to_thread(
        canva_worker.submit, "login", {"email": str(data.get("email", "")).strip()}, 150)
    print("[canva] login res:", res, flush=True)
    return JSONResponse(res)


@app.post("/api/canva/otp")
async def canva_otp(request: Request):
    if not admin_ok(request):
        return JSONResponse({"status": "fail", "message": "Admin login karo"}, status_code=401)
    data = await request.json()
    import canva_worker
    res = await asyncio.to_thread(
        canva_worker.submit, "otp", {"otp": str(data.get("otp", ""))}, 120)
    print("[canva] otp res:", res, flush=True)
    return JSONResponse(res)


# ---------------------------------------------------------------- public API (API key se)

def api_key_ok(request: Request) -> bool:
    key = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    keys = CONFIG.get("api_keys", [])
    return bool(keys) and key in keys


@app.post("/v1/approve")
async def v1_approve(request: Request):
    """API key se code approve: POST /v1/approve  (X-API-Key header)  {"code":"9V6A3T"}"""
    if not api_key_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid API key"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON body chahiye"}, status_code=400)
    code = str(data.get("code", "")).strip().upper().replace(" ", "")
    if not re.match(r"^[A-Z0-9]{6}$", code):
        return JSONResponse({"ok": False, "error": "Code 6 letters/digits ka hona chahiye"}, status_code=400)
    buyer = str(data.get("buyer", ""))[:60]
    rid = uuid.uuid4().hex[:10]
    with db() as conn:
        conn.execute(
            "INSERT INTO requests (id, code, buyer, ip, created_at) VALUES (?,?,?,?,?)",
            (rid, code, buyer, request.client.host if request.client else "?", time.time()),
        )
    import surfshark_api
    result = await asyncio.to_thread(surfshark_api.approve, code)
    if result == "approved":
        with db() as conn:
            conn.execute("UPDATE requests SET status='approved', updated_at=? WHERE id=?",
                         (time.time(), rid))
    mapping = {
        "approved": ("ok", "Code approve ho gaya - client login ho jayega"),
        "invalid": ("fail", "Code ghalat ya expired hai"),
        "no_session": ("fail", "Backend login nahi hai - owner ko batao"),
        "network_error": ("fail", "Surfshark API tak nahi pahunch sakte"),
    }
    if result in mapping:
        ok, msg = mapping[result]
        return JSONResponse({"ok": ok == "ok", "result": result, "message": msg, "id": rid})
    return JSONResponse({"ok": False, "result": result, "id": rid}, status_code=502)


@app.get("/v1/status")
async def v1_status(request: Request):
    if not api_key_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid API key"}, status_code=401)
    import surfshark_api
    info = surfshark_api.account_info()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM requests WHERE status='approved'").fetchone()[0]
    return {
        "ok": True,
        "surfshark_logged_in": bool(info),
        "email": (surfshark_api.SESSION.email or "") if info else "",
        "total_codes": total,
        "approved_codes": approved,
    }


# ---------------------------------------------------------------- canva public API (Surfshark /v1 jaisa)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def _canva_session_ready() -> bool:
    return (DATA_DIR / "canva_session.json").exists()


@app.post("/v1/canva/invite")
async def v1_canva_invite(request: Request):
    """API se Canva invite: POST /v1/canva/invite (X-API-Key) {"email":"x@y.com","buyer":"@u"}"""
    if not api_key_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid API key"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON body chahiye"}, status_code=400)
    email = str(data.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"ok": False, "error": "Valid email chahiye"}, status_code=400)
    if not _canva_session_ready():
        return JSONResponse({"ok": False, "error": "Canva session nahi - owner /canva pe login kare",
                             "status": "no_session"}, status_code=200)
    buyer = str(data.get("buyer", ""))[:60]
    jid = uuid.uuid4().hex[:10]
    with db() as conn:
        conn.execute(
            "INSERT INTO canva_jobs (id, email, buyer, chat_id, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (jid, email, buyer, "", "api", time.time()),
        )
    import canva_worker
    canva_worker.process_job(jid, email, buyer, "")  # chat_id khali = koi TG msg nahi
    return {"ok": True, "id": jid, "status": "processing",
            "message": "Invite process ho rahi hai - GET /v1/canva/job?id=" + jid + " se status poocho"}


@app.get("/v1/canva/job")
async def v1_canva_job(request: Request, id: str = ""):
    """Canva invite ka status: GET /v1/canva/job?id=xxx (X-API-Key)"""
    if not api_key_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid API key"}, status_code=401)
    if not id:
        return JSONResponse({"ok": False, "error": "id chahiye"}, status_code=400)
    with db() as conn:
        row = conn.execute("SELECT * FROM canva_jobs WHERE id=?", (id,)).fetchone()
    if not row:
        return JSONResponse({"ok": False, "error": "job nahi mila"}, status_code=404)
    return {"ok": True, "id": row["id"], "email": row["email"],
            "status": row["status"], "note": row["note"],
            "created_at": row["created_at"]}


@app.get("/v1/canva/status")
async def v1_canva_status(request: Request):
    """Canva system health: GET /v1/canva/status (X-API-Key)"""
    if not api_key_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid API key"}, status_code=401)
    email = ""
    try:
        email = (DATA_DIR / "canva_email.txt").read_text(encoding="utf-8").strip()
    except Exception:
        pass
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM canva_jobs").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM canva_jobs WHERE status='sent'").fetchone()[0]
    return {"ok": True, "canva_logged_in": _canva_session_ready(), "email": email,
            "total_invites": total, "sent_invites": sent}


# ---------------------------------------------------------------- canva invites (PC agent ke liye)
# Cloud bot buyer ka email lega aur canva_jobs mein pending job banayega.
# PC agent (canva_agent.py) ye jobs utha kar browser automation se Canva
# ki ASLI email invite bhejta hai aur result wapas post karta hai.

def canva_agent_ok(request: Request) -> bool:
    key = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    agent_key = str(CONFIG.get("canva_agent_key", ""))
    return bool(agent_key) and key == agent_key


async def send_to_chat(chat_id: str, text: str):
    """Kisi bhi Telegram chat ko Bot API se message (buyer result ke liye)."""
    token = CONFIG.get("bot_token", "")
    tg_base = (CONFIG.get("telegram_api_base") or "https://api.telegram.org").rstrip("/")
    if not token or "PASTE" in token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{tg_base}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
    except Exception as exc:
        print("[canva] buyer message error:", exc)


@app.get("/v1/canva/next")
async def canva_next(request: Request):
    """PC agent: oldest pending job claim karo."""
    if not canva_agent_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid agent key"}, status_code=401)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM canva_jobs WHERE status='pending' "
            "ORDER BY created_at ASC LIMIT 1").fetchone()
        if not row:
            return {"ok": True, "job": None}
        conn.execute("UPDATE canva_jobs SET status='claimed', updated_at=? WHERE id=?",
                     (time.time(), row["id"]))
    return {"ok": True, "job": {"id": row["id"], "email": row["email"], "buyer": row["buyer"]}}


@app.post("/v1/canva/result")
async def canva_result(request: Request):
    """PC agent: invite ka result post karo -> buyer ko bata do."""
    if not canva_agent_ok(request):
        return JSONResponse({"ok": False, "error": "Invalid agent key"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "JSON body chahiye"}, status_code=400)
    jid = str(data.get("id", ""))
    ok = bool(data.get("ok"))
    note = str(data.get("note", ""))[:200]
    with db() as conn:
        row = conn.execute("SELECT * FROM canva_jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "job nahi mila"}, status_code=404)
        conn.execute("UPDATE canva_jobs SET status=?, note=?, updated_at=? WHERE id=?",
                     ("sent" if ok else "failed", note, time.time(), jid))
    email = row["email"]
    if ok:
        await send_to_chat(row["chat_id"],
                           "━━━━━━━━━━━━━━━━━━\n"
                           "✅ CANVA INVITE SENT\n"
                           "━━━━━━━━━━━━━━━━━━\n\n"
                           f"📧 {email} par Canva ki taraf se team invite bhej di gayi hai.\n\n"
                           "Next steps:\n"
                           "1. Apna email inbox kholo (spam folder bhi check karo)\n"
                           "2. Canva ki invite pe **Accept** dabao\n"
                           "3. Canva kholo — Pro features ready hain! 🎉")
        await notify_owner(f"✅ CANVA INVITE SENT\nEmail: {email}\nBuyer: {row['buyer'] or '?'}\n{note}")
    else:
        await send_to_chat(row["chat_id"],
                           "⏳ Canva invite abhi process nahi ho saki — owner ko bata diya gaya "
                           "hai, thori der mein dobara try karo.")
        await notify_owner(f"❌ CANVA INVITE FAIL\nEmail: {email}\nBuyer: {row['buyer'] or '?'}\n{note}")
    return {"ok": True}


@app.post("/admin/login")
async def admin_login(request: Request):
    data = await request.json()
    if str(data.get("pass", "")) != CONFIG.get("admin_pass", ""):
        return JSONResponse({"ok": False}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("admin", CONFIG.get("admin_pass", ""), httponly=True, samesite="lax")
    return resp


@app.get("/api/requests")
async def requests_list(request: Request):
    if not admin_ok(request):
        return JSONResponse({"ok": False}, status_code=401)
    now = time.time()
    with db() as conn:
        conn.execute(
            "UPDATE requests SET status='expired', updated_at=? "
            "WHERE status='pending' AND created_at < ?",
            (now, now - CODE_TTL),
        )
        rows = conn.execute(
            "SELECT id, code, buyer, created_at, status FROM requests "
            "ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    return {"ok": True, "requests": [dict(r) for r in rows]}


@app.post("/admin/status")
async def admin_status(request: Request):
    if not admin_ok(request):
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    rid = str(data.get("id", ""))
    status = str(data.get("status", ""))
    if status not in ("approved", "rejected", "pending"):
        return JSONResponse({"ok": False, "error": "Ghalat status."}, status_code=400)
    with db() as conn:
        conn.execute(
            "UPDATE requests SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), rid),
        )
    return {"ok": True}
