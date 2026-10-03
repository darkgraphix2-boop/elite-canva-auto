"""
auto_approve.py - Surfshark UI automation
-----------------------------------------
Client ka login code aate hi Surfshark app ko khud chala kar
Settings > My Account > Enter login code mein code daal deta hai.
Surfshark app PC pe logged-in rehni chahiye (session app khud
save karti hai - ek dafa login, hamesha ke liye).

Result strings:
  approved      - code chal gaya, screen se navigate ho gaya (client login ho gaya)
  invalid_code  - Surfshark ne "valid code" error diya (code ghalat/expired)
  no_app        - Surfshark app chal nahi rahi
  unknown       - result confirm nahi hua (owner khud check kare)
  error:...     - automation ke doran masla
"""

import threading
import time
from pathlib import Path

import pyautogui

SHOTS_DIR = Path(__file__).parent / "shots"
SUBMIT_WAIT = 4          # submit ke baad kitna intezar (seconds)
LOCK = threading.Lock()  # ek waqt mein ek approval

pyautogui.FAILSAFE = True


def _find_win():
    from pywinauto import Desktop
    for w in Desktop(backend="uia").windows():
        if (w.window_text() or "").startswith("Surfshark"):
            return w
    return None


def _rect_center(el):
    r = el.element_info.rectangle
    cx, cy = r.left + r.width() // 2, r.top + r.height() // 2
    # sanity: screen ke andar aur corners (fail-safe zone) se door
    import ctypes
    user32 = ctypes.windll.user32
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    if cx < 5 or cy < 5 or cx > sw - 5 or cy > sh - 5:
        raise RuntimeError(f"element ki coordinates screen se bahar: ({cx},{cy})")
    return cx, cy


def _visible_matches(win, title=None, ctype=None):
    matches = win.descendants(title=title, control_type=ctype)
    out = []
    try:
        wl, wt = win.rectangle().left, win.rectangle().top
        ww, wh = win.rectangle().width(), win.rectangle().height()
    except Exception:
        return [m for m in matches]
    for m in matches:
        try:
            r = m.element_info.rectangle
            if r.width() > 10 and r.height() > 5 and r.left >= wl - 5 and r.top >= wt - 5 \
                    and r.left < wl + ww and r.top < wt + wh:
                out.append(m)
        except Exception:
            continue
    return out or matches


def _click(win, title=None, ctype=None, hover_expand=False):
    matches = _visible_matches(win, title, ctype)
    if not matches:
        raise RuntimeError(f"element nahi mila: {title or ctype}")
    cx, cy = _rect_center(matches[0])
    pyautogui.moveTo(cx, cy, duration=0.2)
    if hover_expand:
        time.sleep(0.7)
        matches = _visible_matches(win, title, ctype)
        if not matches:
            raise RuntimeError(f"element gayab ho gaya: {title}")
        cx, cy = _rect_center(matches[0])
    pyautogui.click(cx, cy)
    time.sleep(1.0)


def _wait_for(win, text, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has(win, text):
            return True
        time.sleep(0.3)
    return False


def _has(win, text):
    try:
        return any(text in (el.element_info.name or "") for el in win.descendants())
    except Exception:
        return False


def _restore(win):
    import ctypes
    hwnd = win.handle
    if ctypes.windll.user32.IsIconic(hwnd):
        ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.6)


def _shot(win, name):
    try:
        r = win.rectangle()
        SHOTS_DIR.mkdir(exist_ok=True)
        pyautogui.screenshot(region=(r.left, r.top, r.width(), r.height())).save(SHOTS_DIR / name)
    except Exception:
        pass


def _back_from_code_screen(win):
    # code screen ke top-left wala back button (rel ~96,68)
    try:
        r = win.rectangle()
        pyautogui.click(r.left + 96, r.top + 68)
        time.sleep(1.0)
    except Exception:
        pass


def approve_code(code: str) -> str:
    with LOCK:
        # mouse fail-safe zone se hatao (corner pe ho to automation crash hoti hai)
        sw = sh = None
        try:
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            pyautogui.moveTo(sw // 2, sh // 2, duration=0.15)
        except Exception:
            pass

        def _center_mouse():
            if sw and sh:
                try:
                    pyautogui.moveTo(sw // 2, sh // 2, duration=0.15)
                except Exception:
                    pass

        try:
            return _approve_inner(code.strip().upper())
        except pyautogui.FailSafeException:
            _center_mouse()
            time.sleep(0.5)
            try:
                return _approve_inner(code.strip().upper())
            except Exception as exc:
                return f"error:{exc}"
        except Exception as exc:
            return f"error:{exc}"


def _edit_value(edit):
    try:
        return edit.get_value()
    except Exception:
        try:
            return (edit.legacy_properties() or {}).get("Value", "")
        except Exception:
            return None


def _red_error_visible(win):
    """Surfshark error text/border UIA mein nahi aata - red pixels se detect karo."""
    try:
        r = win.rectangle()
        band_x, band_y = r.left + 250, r.top + 260
        band_w, band_h = min(800, r.width() - 260), 180
        img = pyautogui.screenshot(region=(band_x, band_y, band_w, band_h))
        px = img.load()
        for x in range(0, band_w, 3):
            for y in range(0, band_h, 3):
                rr, gg, bb = px[x, y][:3]
                if rr > 170 and gg < 120 and bb < 120:
                    return True
    except Exception:
        pass
    return False


def _approve_inner(code: str) -> str:
    win = _find_win()
    if win is None:
        return "no_app"
    _restore(win)
    old_mouse = pyautogui.position()

    try:
        win.set_focus()
        time.sleep(0.6)

        # agar pehle se code screen pe hain to navigation skip
        if not _wait_for(win, "Confirm login code", 1.0):
            # 1) Settings page
            if not _has(win, "VPN settings"):
                _click(win, title="Settings", ctype="RadioButton", hover_expand=True)
                if not _wait_for(win, "VPN settings", 10.0):
                    _shot(win, "fail_settings.png")
                    return "error:settings page nahi khula"
            # 2) My account (retry ke saath)
            for attempt in range(2):
                _click(win, title="My account", ctype="Text")
                if _wait_for(win, "Enter login code", 8.0):
                    break
            else:
                _shot(win, "fail_account.png")
                return "error:my account nahi khula"
            # 3) Enter login code
            _click(win, title="Enter login code", ctype="Text")

        if not _wait_for(win, "Confirm login code", 8.0):
            _shot(win, "fail_codescreen.png")
            return "error:login code screen nahi khuli"

        # 4) code type karo (verify ke saath - dobara koshish agar type nahi hua)
        edits = _visible_matches(win, ctype="Edit")
        if not edits:
            _shot(win, "fail_edit.png")
            return "error:code input nahi mila"
        typed = False
        for attempt in range(2):
            cx, cy = _rect_center(edits[0])
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            pyautogui.typewrite(code, interval=0.06)
            time.sleep(0.4)
            if _edit_value(edits[0]) == code:
                typed = True
                break
        if not typed:
            _shot(win, "fail_typing.png")
            return "error:code type nahi ho paya"

        # 5) Log in
        _click(win, title="Confirm login code", ctype="Button")

        # 6) result - 8s tak poll karo
        deadline = time.time() + 8
        while time.time() < deadline:
            if not _has(win, "Confirm login code"):
                return "approved"  # button gayab = navigate ho gaya = login ho gaya
            time.sleep(0.5)

        # ab bhi code screen pe hain -> reject hua (error red pixels se confirm)
        invalid = _red_error_visible(win)
        _shot(win, "result_invalid.png" if invalid else "result_unknown.png")
        _back_from_code_screen(win)
        return "invalid_code" if invalid else "unknown"
    finally:
        try:
            pyautogui.moveTo(*old_mouse, duration=0.2)
        except Exception:
            pass
