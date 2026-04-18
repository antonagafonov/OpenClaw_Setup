#!/usr/bin/env python3
"""
HiBob timesheet agent — login and fill weekly working hours on app.hibob.com

First-time setup:
  hibob.py configure --email X --password Y [--start 09:00] [--end 18:00]

Commands:
  hibob.py login            Login with saved credentials, save session
  hibob.py status           Check current auth status
  hibob.py whoami           Show logged-in user info
  hibob.py show_week        Show current week's timesheet
  hibob.py fill_week        Fill current (or last) week with configured hours
  hibob.py logout           Remove saved session

Configuration (~/.hibob/config.json):
  email        Your HiBob login email
  password     Your HiBob password
  start_time   Default start time (HH:MM, default 09:00)
  end_time     Default end time (HH:MM, default 18:00)
  work_days    List of weekday numbers (0=Sun..6=Sat, default [0,1,2,3,4])
  location     Location tag (e.g. "Office", default "Office")
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

SITE_URL = "https://app.hibob.com"

STATE_DIR = Path.home() / ".hibob"
STATE_DIR.mkdir(exist_ok=True)
CONFIG_FILE = STATE_DIR / "config.json"
SESSION_FILE = STATE_DIR / "session.json"

_defaults = {
    "email": "",
    "password": "",
    "start_time": "09:00",
    "end_time": "18:00",
    "work_days": [0, 1, 2, 3, 4],  # Sun-Thu (Israel work week)
    "location": "Office"
}


def _load_config():
    if CONFIG_FILE.exists():
        try:
            return {**_defaults, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return dict(_defaults)


def _save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG_FILE.chmod(0o600)


def load_json(path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def require_session():
    state = load_json(SESSION_FILE)
    if not state:
        print("[hibob] No session. Run 'hibob.py login'.", file=sys.stderr)
        sys.exit(1)
    return state


async def _open_browser(session_state=None):
    p = await async_playwright().start()
    browser = await p.firefox.launch(headless=True)
    ctx_args = {}
    if session_state:
        ctx_args["storage_state"] = session_state
    context = await browser.new_context(**ctx_args)
    page = await context.new_page()
    return p, browser, context, page


# ─── Login ───────────────────────────────────────────────────────────────────

async def cmd_login():
    cfg = _load_config()
    email = cfg.get("email")
    password = cfg.get("password")
    if not email or not password:
        print("[hibob] No credentials. Run: hibob.py configure --email X --password Y", file=sys.stderr)
        sys.exit(1)

    print(f"[hibob] Logging in as {email}...")
    p, browser, context, page = await _open_browser()
    try:
        await page.goto(SITE_URL + "/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # Fill email, click continue
        await page.fill('input[name=email]', email)
        for btn in await page.query_selector_all('button'):
            t = (await btn.text_content() or '').strip().lower()
            if t in ('continue', 'next', 'log in', 'sign in') and await btn.is_visible():
                await btn.click()
                break
        await page.wait_for_timeout(2500)

        # Fill password
        pwd = await page.query_selector('input[name=password]')
        if not pwd:
            print("[hibob] Could not find password field.", file=sys.stderr)
            await browser.close(); await p.stop(); sys.exit(1)
        await pwd.fill(password)

        for btn in await page.query_selector_all('button'):
            t = (await btn.text_content() or '').strip().lower()
            if t in ('log in', 'sign in', 'continue', 'submit') and await btn.is_visible():
                await btn.click()
                break
        await page.wait_for_timeout(5000)

        # Check if logged in
        if '/login' in page.url:
            content = await page.content()
            print(f"[hibob] Login failed. Still at: {page.url}", file=sys.stderr)
            if 'incorrect' in content.lower() or 'invalid' in content.lower():
                print("[hibob] Incorrect credentials.", file=sys.stderr)
            await browser.close(); await p.stop(); sys.exit(1)

        # Save full session state
        state = await context.storage_state()
        save_json(SESSION_FILE, state)
        print(f"[hibob] Login OK. Session saved to {SESSION_FILE}")
        print(f"[hibob] Current URL: {page.url}")
    finally:
        await browser.close()
        await p.stop()


def cmd_status():
    session = load_json(SESSION_FILE)
    if not session:
        print("[hibob] Not logged in.")
        return
    cookies = session.get("cookies", [])
    print(f"[hibob] Session present: {len(cookies)} cookies")
    cfg = _load_config()
    if cfg.get("email"):
        print(f"[hibob] Email: {cfg['email']}")


def cmd_logout():
    SESSION_FILE.unlink(missing_ok=True)
    print("[hibob] Logged out.")


# ─── Main ────────────────────────────────────────────────────────────────────

def usage_and_exit():
    print(__doc__)
    sys.exit(0)


async def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        usage_and_exit()

    cmd = args[0]

    if cmd == "configure":
        cfg = _load_config()
        i = 1
        while i < len(args):
            if args[i] == "--email" and i + 1 < len(args):
                cfg["email"] = args[i + 1]; i += 2
            elif args[i] == "--password" and i + 1 < len(args):
                cfg["password"] = args[i + 1]; i += 2
            elif args[i] == "--start" and i + 1 < len(args):
                cfg["start_time"] = args[i + 1]; i += 2
            elif args[i] == "--end" and i + 1 < len(args):
                cfg["end_time"] = args[i + 1]; i += 2
            elif args[i] == "--location" and i + 1 < len(args):
                cfg["location"] = args[i + 1]; i += 2
            else:
                i += 1
        _save_config(cfg)
        masked = {**cfg, "password": "*" * 8 if cfg.get("password") else ""}
        print(f"[hibob] Config saved to {CONFIG_FILE}")
        print(json.dumps(masked, ensure_ascii=False, indent=2))
        return

    elif cmd == "login":
        await cmd_login()
    elif cmd == "status":
        cmd_status()
    elif cmd == "logout":
        cmd_logout()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        usage_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
