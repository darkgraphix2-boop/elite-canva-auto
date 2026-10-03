"""
canva_worker.py - Canva browser automation worker (cloud/PC dono ke liye)
-------------------------------------------------------------------------
Surfshark jaisa flow, lekin Canva ke liye browser automation se:
  submit("login", {email, password}) -> {status: ok|need_otp|fail, ...}
  submit("otp", {code})              -> {status: ok|fail, ...}
  submit("invite", {email})          -> {ok, note, shot} (canva_invite se)

Playwright ka SYNC API sirf EK thread pe chal sakta hai, isliye sab
operations ek dedicated worker thread ke through jate hain. Login ke
waqt browser email->OTP ke beech khula rehta hai (global _login).

Session: DATA_DIR/canva_session.json (Railway volume pe) - PC agent ki
zaroorat nahi, invites bhi cloud se chalti hain.
"""

import concurrent.futures
import json
import queue
import sqlite3
import threading
import time

import httpx

import canva_invite

DATA_DIR = canva_invite.DATA_DIR
SESSION_FILE = canva_invite.SESSION_FILE
EMAIL_FILE = DATA_DIR / "canva_email.txt"

_TASKS = queue.Queue()
_login = {}  # khula login browser (email -> OTP beech mein zinda rehta hai)
_started = False
_start_lock = threading.Lock()


# ------------------------------------------------------------------ helpers

def _cfg(key, default=""):
    try:
        return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8")).get(key, default)
    except Exception:
        return default


def _close_login_browser():
    b = _login.pop("browser", None)
    if b:
        try:
            b.close()
        except Exception:
            pass
    _login.clear()


def _looks_logged_in(page) -> bool:
    """Settings > People page kholo - invite button dikhe to login hai."""
    try:
        return canva_invite._open_settings(page) == ""
    except Exception:
        return False


def _body_text(page) -> str:
    try:
        return (page.text_content("body") or "").lower()
    except Exception:
        return ""


# ------------------------------------------------------------------ login flow

def _do_login(pw, email: str, password: str = "") -> dict:
    """PASSWORDLESS flow: email -> Canva email pe OTP bhejta hai -> need_otp.
    (password wale purane accounts pe password screen ka clear fail aata hai)"""
    _close_login_browser()
    # headless=False ZAROORI (xvfb display) - headless Cloudflare pe atakta hai
    browser = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    context = browser.new_context()
    page = context.new_page()
    _login["browser"] = browser
    _login["context"] = context
    _login["page"] = page
    EMAIL_FILE.write_text(email, encoding="utf-8")

    page.goto("https://www.canva.com/login", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    canva_invite._dismiss_cookies(page)

    # "Continue with email" (email input isi click ke BAAD aata hai)
    email_btn = canva_invite._find_visible(page, page.get_by_role(
        "button", name=canva_invite.re.compile(r"continue with email|log in with email", canva_invite.re.I)))
    if email_btn:
        email_btn.click()
        page.wait_for_timeout(1500)

    inbox = (canva_invite._find_visible(page, page.locator("input[type=email]"))
             or canva_invite._find_visible(page, page.get_by_placeholder(canva_invite.re.compile(r"email", canva_invite.re.I)))
             or canva_invite._find_visible(page, page.locator("input")))
    if not inbox:
        canva_invite._shot(page, "login_no_email_input")
        _close_login_browser()
        return {"status": "fail", "message": "email input nahi mila (UI badla hua hai) - screenshot dekho"}
    inbox.fill(email)
    page.wait_for_timeout(600)

    cont = canva_invite._find_visible(page, page.get_by_role(
        "button", name=canva_invite.re.compile(r"continue|next", canva_invite.re.I)))
    if cont:
        cont.click()
    else:
        inbox.press("Enter")

    # OTP screen ka intezaar (Canva code email pe bhejta hai)
    deadline = time.time() + 30
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        if _detect_otp(page):
            # browser KHLA rehta hai - /api/canva/otp isi mein code daalega
            return {"status": "need_otp",
                    "message": "Canva ne code bheja hai - apna email check karo aur OTP daalo"}
        body = _body_text(page)
        if "create your account" in body or "you're creating a canva account" in body:
            canva_invite._shot(page, "login_signup_screen")
            _close_login_browser()
            return {"status": "fail",
                    "message": "Is email se koi Canva account nahi hai (signup screen aa gayi) - "
                               "wo email dalo jisse Canva account bana hai"}
        if any(w in body for w in ("couldn't find", "no account", "doesn't match",
                                   "incorrect email")):
            canva_invite._shot(page, "login_bad_email")
            _close_login_browser()
            return {"status": "fail", "message": "Ye email Canva pe nahi mila - sahi email dalo"}
        # purana password wala account (uncommon)
        if canva_invite._find_visible(page, page.locator("input[type=password]")):
            canva_invite._shot(page, "login_password_screen")
            _close_login_browser()
            return {"status": "fail",
                    "message": "Ye account password mang raha hai - Canva app mein 'Login with email code' "
                               "wala option use karo ya owner se baat karo"}
    canva_invite._shot(page, "login_timeout")
    _close_login_browser()
    return {"status": "fail", "message": "OTP screen nahi aayi (timeout) - dobara try karo"}


def _detect_otp(page) -> bool:
    """OTP/verification screen hai ya nahi."""
    body = _body_text(page)
    if any(w in body for w in ("enter the code", "verification code", "we sent a code",
                               "6-digit code", "code we sent")):
        return True
    try:
        boxes = page.locator("input[maxlength='1']")
        if boxes.count() >= 4:
            return True
    except Exception:
        pass
    return False


def _do_otp(code: str) -> dict:
    page = _login.get("page")
    if not page:
        return {"status": "fail", "message": "koi login browser khula nahi - pehle login karo"}
    code = (code or "").strip().replace(" ", "")

    boxes = page.locator("input[maxlength='1']")
    filled = False
    try:
        if boxes.count() >= max(len(code), 4):
            for i, ch in enumerate(code):
                boxes.nth(i).fill(ch)
                page.wait_for_timeout(150)
            filled = True
    except Exception:
        pass
    if not filled:
        otp_input = (canva_invite._find_visible(page, page.get_by_placeholder(canva_invite.re.compile(r"code", canva_invite.re.I)))
                     or canva_invite._find_visible(page, page.locator("input[type=tel]"))
                     or canva_invite._find_visible(page, page.locator("input[type=text]"))
                     or canva_invite._find_visible(page, page.locator("input")))
        if not otp_input:
            canva_invite._shot(page, "otp_no_input")
            return {"status": "fail", "message": "OTP input nahi mila - screenshot dekho"}
        otp_input.fill(code)

    page.wait_for_timeout(800)
    verify = (canva_invite._find_visible(page, page.get_by_role(
        "button", name=canva_invite.re.compile(r"verify|confirm|continue|submit|next", canva_invite.re.I)))
        or canva_invite._find_visible(page, page.locator("button[type=submit]")))
    clicked = False
    if verify:
        try:
            verify.click(timeout=8000)
            clicked = True
        except Exception as exc:
            print("[canva] verify click fail:", type(exc).__name__, "- Enter try karta hoon")
    if not clicked:
        page.keyboard.press("Enter")

    deadline = time.time() + 30
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        body = _body_text(page)
        if any(w in body for w in ("invalid", "incorrect code", "expired", "try again")):
            canva_invite._shot(page, "otp_bad")
            _close_login_browser()
            return {"status": "fail", "message": "OTP ghalat ya expire ho gaya - dobara login karo"}
        if "/login" not in (page.url or "") and _looks_logged_in(page):
            email = EMAIL_FILE.read_text(encoding="utf-8").strip() if EMAIL_FILE.exists() else ""
            _finish_login(_login["context"], email)
            return {"status": "ok", "message": "OTP verify - Canva session save ho gayi"}
    canva_invite._shot(page, "otp_timeout")
    _close_login_browser()
    return {"status": "fail", "message": "OTP ke baad login confirm nahi hua (timeout)"}


def _finish_login(context, email: str):
    """storage_state volume pe save karo aur login browser band karo."""
    context.storage_state(path=str(SESSION_FILE))
    if email:
        EMAIL_FILE.write_text(email, encoding="utf-8")
    _close_login_browser()
    print("[canva] session save ho gayi:", SESSION_FILE)


# ------------------------------------------------------------------ invite job

def process_job(jid: str, email: str, buyer: str = "", chat_id: str = ""):
    """Fire-and-forget thread: invite bhejo, DB update karo, buyer+owner ko batao."""
    threading.Thread(target=_process_job, args=(jid, email, buyer, chat_id), daemon=True).start()


def _process_job(jid: str, email: str, buyer: str, chat_id: str):
    res = submit("invite", {"email": email}, timeout=240)
    ok = bool(res.get("ok"))
    note = str(res.get("note", ""))[:200]
    try:
        conn = sqlite3.connect(DATA_DIR / "slots.db")
        with conn:
            conn.execute("UPDATE canva_jobs SET status=?, note=?, updated_at=? WHERE id=?",
                         ("sent" if ok else "failed", note, time.time(), jid))
        conn.close()
    except Exception as exc:
        print("[canva] db update error:", exc)

    if ok:
        _bot_send(chat_id,
                  "━━━━━━━━━━━━━━━━━━\n"
                  "✅ CANVA INVITE SENT\n"
                  "━━━━━━━━━━━━━━━━━━\n\n"
                  f"📧 {email} par Canva ki taraf se team invite bhej di gayi hai.\n\n"
                  "Next steps:\n"
                  "1. Apna email inbox kholo (spam folder bhi check karo)\n"
                  "2. Canva ki invite pe Accept dabao\n"
                  "3. Canva kholo — Pro features ready hain! 🎉")
        _bot_send(_cfg("owner_chat_id"),
                  f"✅ CANVA INVITE SENT\nEmail: {email}\nBuyer: {buyer or '?'}\n{note}")
    else:
        _bot_send(chat_id,
                  "⏳ Canva invite abhi process nahi ho saki — owner ko bata diya gaya "
                  "hai, thori der mein dobara try karo.")
        _bot_send(_cfg("owner_chat_id"),
                  f"❌ CANVA INVITE FAIL\nEmail: {email}\nBuyer: {buyer or '?'}\n{note}")


def _bot_send(chat_id: str, text: str):
    token = _cfg("bot_token")
    if not token or not chat_id:
        return
    try:
        httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as exc:
        print("[canva] telegram send error:", exc)


# ------------------------------------------------------------------ worker thread

def _worker():
    # patchright = patched playwright (Cloudflare bypass); warna normal playwright
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        while True:
            kind, payload, fut = _TASKS.get()
            try:
                if kind == "login":
                    res = _do_login(pw, payload["email"], payload.get("password", ""))
                elif kind == "otp":
                    res = _do_otp(payload.get("otp") or payload.get("code") or "")
                elif kind == "team_inspect":
                    res = _team_inspect(pw)
                elif kind == "invite":
                    res = canva_invite._invite_impl(pw, payload["email"], headless=False)
                else:
                    res = {"status": "fail", "ok": False, "message": "unknown task"}
            except Exception as exc:
                res = {"status": "fail", "ok": False,
                       "message": f"{type(exc).__name__}: {str(exc)[:150]}",
                       "note": f"{type(exc).__name__}: {str(exc)[:150]}"}
            fut.set_result(res)


def submit(kind: str, payload: dict, timeout: int = 180) -> dict:
    global _started
    with _start_lock:
        if not _started:
            threading.Thread(target=_worker, daemon=True).start()
            _started = True
    fut = concurrent.futures.Future()
    _TASKS.put((kind, payload, fut))
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return {"status": "fail", "ok": False,
                "message": "browser task timeout - dobara try karo",
                "note": "worker timeout"}


def _team_inspect(pw):
    browser = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()
    try:
        page.goto("https://www.canva.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        canva_invite._dismiss_cookies(page)
        controls = page.locator("button, [role=button]").evaluate_all("(els)=>els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({text:(e.innerText||'').trim(),label:e.getAttribute('aria-label'),testid:e.getAttribute('data-testid')}))")
        for pattern in [r"profile", r"your account", r"account menu", r"account", r"switch team"]:
            button = canva_invite._find_visible(page, page.get_by_role("button", name=canva_invite.re.compile(pattern, canva_invite.re.I)))
            if button:
                button.click(timeout=5000)
                page.wait_for_timeout(1200)
                break
        personal = canva_invite._find_visible(page, page.get_by_text("Personal", exact=True))
        before = personal.evaluate("(e)=>e.parentElement.parentElement.parentElement.outerHTML") if personal else ""
        if personal:
            personal.click(timeout=5000)
            page.wait_for_timeout(1500)
        return {"ok": True, "team_html":before, "after_html":page.locator("[role=dialog],[role=menu],[role=listbox]").evaluate_all("(es)=>es.filter(e=>e.getBoundingClientRect().width>0).map(e=>e.outerHTML)"), "url":page.url, "controls":controls, "body":(page.inner_text("body") or "")[:12000], "menu":page.locator("[role=menuitem], [role=option], [role=menuitemradio], [role=menuitemcheckbox], button").evaluate_all("(els)=>els.filter(e=>e.getBoundingClientRect().width>0).map(e=>({text:(e.innerText||'').trim(),label:e.getAttribute('aria-label'),role:e.getAttribute('role'),checked:e.getAttribute('aria-checked'),selected:e.getAttribute('aria-selected')}))")}
    finally:
        context.close()
        browser.close()
