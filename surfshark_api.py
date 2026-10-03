"""
surfshark_api.py - Surfshark ka headless (bina-app) client
----------------------------------------------------------
Telegram bot jaisa flow:
  login(email, password)  -> ya to token mil jata hai, ya OTP mangta hai
  verify_otp(6-digit)     -> token mil jata hai
  token session.json mein save (elite_user.session jaisa)
  renew()                 -> token fresh rakhta hai
  approve(code)           -> client ka 6-letter login code approve

Ye Surfshark ke mobile API (v1) se baat karta hai - PC, app,
mouse automation kuch nahi chahiye. Kisi bhi server (VPS) pe chalta hai.
"""

import json
import os
import threading
import time
from pathlib import Path

import httpx

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ELITE_DATA_DIR") or BASE)
SESSION_PATH = DATA_DIR / "session.json"

API_DEFAULT = "https://api.surfshark.com/v1"
UA = "Surfshark/2.24.0 (com.surfshark.vpnclient.ios; build:19; iOS 16.0) Alamofire/5.4.3 device/mobile"


def _api_base() -> str:
    """config.json ka 'api_base' (jaise Cloudflare Worker URL) - ISP block bypass."""
    try:
        cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        base = (cfg.get("api_base") or "").strip().rstrip("/")
        if base:
            return base
    except Exception:
        pass
    return API_DEFAULT

# login ke doran Sirf EK baar password memory mein aata hai, save nahi hota.
# session.json mein sirf token + renewToken jata hai (password kabhi nahi).

# RLock zaroori hai: approve() ke andar renew() dobara lock leta hai
# (plain Lock pe DEADLOCK hota - pehle runtime patch se fix hota tha)
_lock = threading.RLock()

LAST_APPROVE = {}  # aakhri approve ki raw response (device info waghera ke liye)


class Session:
    def __init__(self):
        self.token = None
        self.renew_token = None
        self.email = None
        self.saved_at = 0
        self._load()

    def _load(self):
        if SESSION_PATH.exists():
            try:
                d = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
                self.token = d.get("token")
                self.renew_token = d.get("renewToken")
                self.email = d.get("email")
                self.saved_at = d.get("savedAt", 0)
            except Exception:
                pass

    def save(self):
        d = {"token": self.token, "renewToken": self.renew_token,
             "email": self.email, "savedAt": time.time()}
        SESSION_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")

    def clear(self):
        self.token = self.renew_token = self.email = None
        self.saved_at = 0
        try:
            SESSION_PATH.unlink()
        except FileNotFoundError:
            pass

    @property
    def logged_in(self):
        return bool(self.token)


SESSION = Session()


def _client():
    return httpx.Client(
        base_url=_api_base(),
        headers={"User-Agent": UA, "Content-Type": "application/json;charset=utf-8",
                 "Accept": "application/json"},
        timeout=20,
    )


def login(email: str, password: str):
    """Returns: (status, message)
    status: 'ok' | 'need_otp' | 'fail'
    """
    with _lock:
        with _client() as c:
            try:
                r = c.post("/auth/login", json={"username": email, "password": password})
            except httpx.HTTPError as e:
                return "fail", f"Network error: {type(e).__name__} - Surfshark API tak nahi pahunch sakte. (VPN/offline check karo)"

            if r.status_code == 200:
                d = r.json()
                SESSION.token = d.get("token")
                SESSION.renew_token = d.get("renewToken")
                SESSION.email = email
                SESSION.save()
                return "ok", "Login ho gaya!"
            if r.status_code == 423:
                # 2FA enabled - body mein pre-auth token aata hai (OTP ke liye zaroori)
                try:
                    d = r.json()
                    SESSION.token = d.get("token")
                    SESSION.renew_token = d.get("renewToken")
                except Exception:
                    SESSION.token = None
                    SESSION.renew_token = None
                SESSION.email = email
                SESSION.save()
                return "need_otp", "OTP chahiye - apne email/authenticator se 6-digit code lo"
            if r.status_code == 401:
                return "fail", "Email ya password ghalat hai"
            if r.status_code == 429:
                return "fail", "Bahut zyada koshish - 5 minute baad try karo"
            return "fail", f"Surfshark ne jawab diya: {r.status_code} {r.text[:120]}"


def verify_otp(otp: str):
    """2FA ka OTP submit karo (pre-auth token Authorization mein chahiye).
    Returns (status, message)."""
    if not SESSION.token:
        return "fail", "Pehle email+password se login karo, phir OTP aayega"
    with _lock:
        with _client() as c:
            try:
                r = c.post("/auth/activate",
                           headers={"Authorization": f"Bearer {SESSION.token}"},
                           json={"otp": otp.strip()})
            except httpx.HTTPError as e:
                return "fail", f"Network error: {type(e).__name__}"

            if r.status_code == 204:
                return "ok", "OTP verify ho gaya - session active!"
            if r.status_code == 403 or (r.status_code == 400 and "Invalid OTP" in r.text):
                return "fail", "OTP ghalat hai - dobara try karo"
            return "fail", f"Surfshark ne jawab diya: {r.status_code} {r.text[:120]}"


def renew() -> bool:
    """renewToken se fresh token lo (session zinda rakhne ke liye)."""
    if not SESSION.renew_token:
        return False
    with _lock:
        with _client() as c:
            try:
                r = c.post("/auth/renew",
                           headers={"Authorization": f"Bearer {SESSION.renew_token}"})
            except httpx.HTTPError:
                return False
            if r.status_code == 200:
                d = r.json()
                if d.get("token"):
                    SESSION.token = d["token"]
                    SESSION.renew_token = d.get("renewToken", SESSION.renew_token)
                    SESSION.save()
                    return True
            return False


def account_info():
    """Logged-in account ki info. Returns dict ya None (agar session mara hai)."""
    if not SESSION.logged_in:
        SESSION._load()
    if not SESSION.logged_in:
        return None
    with _client() as c:
        for attempt in range(2):
            try:
                r = c.get("/account/users/me",
                          headers={"Authorization": f"Bearer {SESSION.token}"})
            except httpx.HTTPError:
                return None
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401 and attempt == 0:
                if renew():
                    continue
                SESSION.clear()
                return None
            return None
    return None


def approve(code: str) -> str:
    """Client ka 6-letter login code approve karo.
    Returns: 'approved' | 'invalid' | 'no_session' | 'network_error' | 'error:...'
    """
    code = code.strip().upper()
    if not SESSION.logged_in:
        # doosre process (website) ne login kiya ho to disk se fresh utha lo
        SESSION._load()
    if not SESSION.logged_in:
        return "no_session"
    with _lock:
        with _client() as c:
            for attempt in range(3):
                try:
                    r = c.post("/account/authorization/assign",
                               headers={"Authorization": f"Bearer {SESSION.token}"},
                               json={"code": code})
                except httpx.HTTPError as e:
                    return "network_error"
                if r.status_code == 200:
                    try:
                        body = r.json()
                    except Exception:
                        body = r.text[:400]
                    LAST_APPROVE.clear()
                    LAST_APPROVE.update({"body": body, "at": time.time()})
                    print("[surfshark] assign 200 body:", r.text[:300])
                    return "approved"
                if r.status_code == 401:
                    if attempt == 0:
                        if renew():
                            continue
                    # shayad doosre process ne renew kiya ho - disk se fresh lo
                    SESSION._load()
                    if SESSION.token and attempt < 2:
                        continue
                    return "no_session"
                if r.status_code in (400, 404, 422):
                    return "invalid"  # code ghalat/expired
                return f"error:{r.status_code} {r.text[:100]}"
    return "invalid"
