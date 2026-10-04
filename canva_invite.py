"""
canva_invite.py - Canva team email invite automation (Playwright)
-----------------------------------------------------------------
Canva ka official API team invite support nahi karta, isliye ye
browser automation hai (Canva ki ASLI email invite jati hai).

Do modes:

  python canva_invite.py login
      Browser khulega - EK DAFa apne Canva account mein login karo.
      Login complete hote hi session save ho jayegi (canva_session.json)
      aur window band ho jayegi. Ye sirf ek dafa karna hai.

  python canva_invite.py invite <email> [--debug]
      Saved session se Canva kholo aur <email> ko team invite bhejo.
      --debug = browser visible (selectors debug karne ke liye)

Fail hone par shots/ folder mein screenshot bachti hai (debugging).

NOTE: Canva UI English mein honi chahiye (selectors text-based hain).
"""

import json
import os
import re
import sys
import time
from pathlib import Path

try:
    # patchright = patched playwright (Cloudflare bypass); warna normal playwright
    from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ELITE_DATA_DIR") or BASE)
SESSION_FILE = DATA_DIR / "canva_session.json"
SHOTS = DATA_DIR / "shots"
SHOTS.mkdir(exist_ok=True)

CANVA_HOME = "https://www.canva.com/"
SETTINGS_URLS = [
    "https://www.canva.com/settings/people-and-permissions",
    "https://www.canva.com/settings/people",
]

INVITE_BTN_TEXTS = re.compile(r"^(invite people|invite members|invite for free|invite via email|add people|invite)$", re.I)
SEND_BTN_TEXTS = re.compile(r"^(send|send invitations?|send invites?|invite for free|add|invite)$", re.I)
SUCCESS_TEXT = re.compile(r"(invitation sent|invite sent|has been sent|invited)", re.I)


def _shot(page, tag: str) -> str:
    try:
        path = SHOTS / f"canva_{tag}_{int(time.time())}.png"
        page.screenshot(path=str(path))
        return path.name
    except Exception:
        return ""


def _find_visible(page, locator):
    """Pehla VISIBLE match wapas lata hai (ya None)."""
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count):
        el = locator.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def _looks_logged_out(page) -> bool:
    url = page.url or ""
    if "/login" in url or "auth" in url:
        return True
    for sel in ["text=Log in", "text=Sign up", "text=Get started"]:
        try:
            if page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            continue
    return False


def _dismiss_cookies(page):
    """Canva ka cookie banner hatao - warna clicks intercept hote hain."""
    try:
        for btn in page.query_selector_all("button"):
            t = (btn.text_content() or "").strip().lower()
            if t in ("accept all cookies", "accept all", "accept cookies"):
                btn.click()
                page.wait_for_timeout(1200)
                return True
    except Exception:
        pass
    return False



def _find_send_button(page, inbox):
    """Find the final submit control near the recipient field."""
    scope = inbox
    for _ in range(24):
        scope = scope.locator("xpath=..")
        button = _find_visible(page, scope.get_by_role("button", name=SEND_BTN_TEXTS))
        if button:
            return button
    # Canva can render the submit control in a separate portal. Only accept
    # a single visible matching control; ambiguous candidates stop the invite.
    candidates = page.get_by_role("button", name=SEND_BTN_TEXTS)
    visible = [candidates.nth(i) for i in range(candidates.count())
               if candidates.nth(i).is_visible()]
    return visible[0] if len(visible) == 1 else None


def _open_settings(page) -> str:
    """Settings > People page kholo. Return '' ya error."""
    last_err = ""
    for url in SETTINGS_URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        except Exception as exc:
            last_err = f"goto fail: {exc}"
            continue
        _dismiss_cookies(page)
        if _looks_logged_out(page):
            return "SESSION DEAD - /canva page se dobara login karo"
        # People page pe 'Invite' type button dikhna chahiye
        try:
            page.wait_for_selector("button", timeout=10000)
        except Exception:
            pass
        btn = _find_visible(page, page.get_by_role("button", name=INVITE_BTN_TEXTS))
        if btn:
            return ""
        last_err = f"is URL pe invite button nahi mila: {page.url}"
    return last_err or "invite button kahin nahi mila"


def invite(email: str, debug: bool = False) -> dict:
    """Ek email ko team invite bhejo. Returns {ok, note, shot}."""
    with sync_playwright() as p:
        return _invite_impl(p, email, debug=debug)


def _invite_impl(pw, email: str, debug: bool = False, headless: bool = True, team_name: str = "") -> dict:
    """invite() ka asal kaam - pw bahar se bhi de sakte hain (worker thread)."""
    email = (email or "").strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
        return {"ok": False, "note": "email format ghalat hai", "shot": ""}
    if not SESSION_FILE.exists():
        return {"ok": False,
                "note": "canva_session.json nahi - website /canva pe Canva login karo",
                "shot": ""}

    browser = pw.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    context = browser.new_context(storage_state=str(SESSION_FILE))
    page = context.new_page()
    try:
        import canva_teams
        err = canva_teams.open_team_settings(page,team_name)
        if err:
            return {"ok":False,"note":err,"shot":_shot(page,"team_selection_failed")}

        # invite dialog kholo
        btn = _find_visible(page, page.get_by_role("button", name=INVITE_BTN_TEXTS))
        btn.click()
        page.wait_for_timeout(1200)

        # email input dhoondo (dialog ke andar)
        inbox = (_find_visible(page, page.get_by_placeholder(re.compile(r"^(?!.*search).*email", re.I)))
                 or _find_visible(page, page.get_by_role("textbox", name=re.compile(r"enter email", re.I)))
                 or _find_visible(page, page.locator("input[type=email]")))
        if not inbox:
            return {"ok": False, "note": "email input nahi mila",
                    "shot": _shot(page, "no_input")}
        inbox.fill(email)
        page.wait_for_timeout(800)

        # role dropdown (agar aaye) - MEMBER default rehta hai, kuch mat karo

        # send dabao
        send = _find_send_button(page, inbox)
        if not send:
            return {"ok": False, "note": "send button nahi mila",
                    "shot": _shot(page, "no_send")}
        send.click()

        # success: toast ya pending list mein email nazar aana
        try:
            page.get_by_text(SUCCESS_TEXT).first.wait_for(state="visible", timeout=8000)
            ok = True
            note = "invite chali gayi"
        except PWTimeout:
            # fallback: email page pe kahin nazar aa raha (pending list)
            try:
                if page.get_by_text(email, exact=True).first.is_visible(timeout=2500):
                    ok = True
                    note = "invite list mein nazar aa gayi"
                else:
                    ok = False
                    note = "success confirm nahi hua (toast nahi mila)"
            except Exception:
                ok = False
                note = "success confirm nahi hua"
        return {"ok": ok, "note": note, "shot": _shot(page, "result")}
    except Exception as exc:
        try:
            shot = _shot(page, "exception")
        except Exception:
            shot = ""
        return {"ok": False, "note": f"{type(exc).__name__}: {str(exc)[:120]}", "shot": shot}
    finally:
        context.close()
        browser.close()


def login(debug: bool = True):
    """Ek dafa login - browser khulta hai, user login karta hai, session save."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not debug)
        context = browser.new_context()
        page = context.new_page()
        page.goto(CANVA_HOME, wait_until="domcontentloaded")
        print("[canva] Browser khul gaya - AB APNE CANVA MEIN LOGIN KARO.")
        print("[canva] Login complete hote hi ye script KHUD save kar ke band ho jayegi (max 10 min).")
        deadline = time.time() + 600
        logged = False
        while time.time() < deadline:
            try:
                if not _looks_logged_out(page):
                    # settings page khol ke pakka check karo
                    chk = context.new_page()
                    chk.goto(SETTINGS_URLS[0], wait_until="domcontentloaded", timeout=30000)
                    if not _looks_logged_out(chk):
                        logged = True
                        chk.close()
                        break
                    chk.close()
            except Exception:
                pass
            time.sleep(3)
        if logged:
            context.storage_state(path=str(SESSION_FILE))
            print(f"[canva] LOGIN OK - session save ho gayi: {SESSION_FILE}")
            print("[canva] Ab agent invites de sakta hai. Agar chal raha hai to restart na karo.")
        else:
            print("[canva] 10 min mein login detect nahi hua - dobara chalao.")
        context.close()
        browser.close()
        return logged


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--debug"]
    dbg = "--debug" in sys.argv
    if not args or args[0] == "login":
        login(debug=True)
    elif args[0] == "invite" and len(args) > 1:
        result = invite(args[1], debug=dbg)
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["ok"] else 1)
    else:
        print("Use: python canva_invite.py login | python canva_invite.py invite <email> [--debug]")

