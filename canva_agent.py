"""
canva_agent.py - Canva invite WORKER (sirf PC pe chalta hai)
------------------------------------------------------------
Cloud bot buyer ke emails ko canva_jobs mein deta hai. Ye agent
10 second mein ek dafa cloud se pending job uthata hai, apne
logged-in Canva browser (canva_invite.py) se ASLI email invite
bhejta hai, aur result wapas cloud ko post karta hai.

Chalana (PC pe, min khula rehna chahiye):
    python canva_agent.py

PEHLE EK DAFa:  python canva_invite.py login   (session save hoti hai)
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ELITE_DATA_DIR") or BASE)
LOG_FILE = DATA_DIR / "canva_agent_log.txt"


def log(msg: str):
    line = f"[agent {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def cfg(key, default=""):
    try:
        c = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        return c.get(key, default)
    except Exception:
        return default


def main():
    cloud = str(cfg("cloud_base") or "").rstrip("/")
    key = str(cfg("canva_agent_key") or "")
    if not cloud or not key:
        log("config.json mein cloud_base / canva_agent_key nahi hai!")
        return
    headers = {"X-API-Key": key}
    log(f"agent shuru - cloud: {cloud}")

    while True:
        try:
            if not (DATA_DIR / "canva_session.json").exists():
                log("canva_session.json nahi - pehle 'python canva_invite.py login' chalao (30s mein dobara check)")
                time.sleep(30)
                continue

            r = httpx.get(f"{cloud}/v1/canva/next", headers=headers, timeout=15)
            data = r.json() if r.status_code == 200 else {}
            job = (data or {}).get("job")

            if not job:
                time.sleep(10)
                continue

            email = job["email"]
            log(f"job {job['id']} aya: {email} - invite bhej raha hoon...")
            import canva_invite
            result = canva_invite.invite(email)
            log(f"result: {result}")

            try:
                httpx.post(f"{cloud}/v1/canva/result", headers=headers,
                           json={"id": job["id"], "ok": result["ok"], "note": result["note"]},
                           timeout=15)
            except Exception as exc:
                log(f"result post fail: {exc}")

            time.sleep(5)
        except KeyboardInterrupt:
            log("band ho gaya")
            return
        except Exception as exc:
            log(f"loop error ({type(exc).__name__}): {str(exc)[:100]} - 15s mein dobara")
            time.sleep(15)


if __name__ == "__main__":
    main()
