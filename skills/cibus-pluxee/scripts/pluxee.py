#!/usr/bin/env python3
"""
Pluxee (Cibus) Agent - Login and account management for consumers.pluxee.co.il

First-time setup:
  pluxee.py configure --phone 05XXXXXXXX   Save your phone number

Two-step login:
  pluxee.py send [--phone PHONE]  Send OTP to phone
  pluxee.py verify <otp>          Verify OTP and save auth token

Account queries (require login):
  pluxee.py balance                         Show account balance/budget
  pluxee.py orders [--count N]              Show last N orders (default: 5)
  pluxee.py restaurants [--type pickup|delivery] [--limit N]  List available restaurants
  pluxee.py menu <restaurant_id> [--type pickup|delivery]     Show restaurant menu
  pluxee.py morning_ping                    Telegram-ready morning summary
  pluxee.py whoami                          Show user info
  pluxee.py logout                          Remove saved token
  pluxee.py status                          Show current auth status

Configuration (~/.pluxee/config.json):
  phone             Your Pluxee phone number
  favorites         Dict of name substrings → display names to watch
  watched_categories  Dict of food_type codes → labels (e.g. "20011": "אוכל ביתי")
  category_max_dist   Max distance in meters for category filter (default: 10000)

Examples:
  python3 pluxee.py configure --phone 0521234567
  python3 pluxee.py send
  python3 pluxee.py verify 825335
  python3 pluxee.py balance
  python3 pluxee.py restaurants
  python3 pluxee.py menu 145267
  python3 pluxee.py morning_ping
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ─── Pluxee API constants ────────────────────────────────────────────────────

SITE_URL = "https://consumers.pluxee.co.il"
CAPIR_BASE_URL = "https://api.capir.pluxee.co.il"
API_DOMAIN = "https://api.consumers.pluxee.co.il/api"
APP_ID = "E5D5FEF5-A05E-4C64-AEBA-BA0CECA0E402"
RECAPTCHA_SITE_KEY = "6LddY28jAAAAALbiEdodIdIYiM563_AgOW4LMcmu"

# ─── User configuration (edit these or use ~/.pluxee/config.json) ────────────

STATE_DIR = Path.home() / ".pluxee"
STATE_DIR.mkdir(exist_ok=True)
CONFIG_FILE = STATE_DIR / "config.json"

_defaults = {
    "phone": "",
    "favorites": {},
    "watched_categories": {
        "20011": "\U0001f3e0 אוכל ביתי"
    },
    "category_max_dist": 10000
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

CONFIG = _load_config()

DEFAULT_PHONE = CONFIG.get("phone", "")

SESSION_FILE = STATE_DIR / "session.json"
TOKEN_FILE = STATE_DIR / "token.json"
BROWSER_STATE_FILE = STATE_DIR / "browser_state.json"
FULL_SESSION_FILE = STATE_DIR / "full_session.json"


def load_json(path):
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def require_session():
    """Return the full browser session state for API calls."""
    state = load_json(FULL_SESSION_FILE)
    if not state:
        print("[pluxee] No session. Run 'pluxee.py send' then 'pluxee.py verify <otp>'.", file=sys.stderr)
        sys.exit(1)
    return state


def _clean_state(session_state):
    """Strip logout cookie so Angular doesn't trigger a logout on page load."""
    clean = dict(session_state)
    clean["cookies"] = [c for c in clean.get("cookies", []) if c.get("name") != "cibus-signed-out"]
    return clean


async def _open_browser(session_state):
    """Open Firefox with session, navigate to base page. Returns (playwright, browser, page)."""
    p = await async_playwright().start()
    browser = await p.firefox.launch(headless=True)
    context = await browser.new_context(storage_state=_clean_state(session_state))
    page = await context.new_page()
    await page.goto(SITE_URL + "/user/orders", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)
    return p, browser, page


async def _post(page, body):
    """POST to main.py API."""
    body_json = json.dumps(body, ensure_ascii=False)
    return await page.evaluate(f"""
        async () => {{
            const resp = await fetch('{API_DOMAIN}/main.py', {{
                method: 'POST', credentials: 'include',
                headers: {{
                    'Content-Type': 'application/json; charset=utf-8',
                    'application-id': '{APP_ID}'
                }},
                body: {repr(body_json)}
            }});
            return await resp.json();
        }}
    """)


async def _get(page, path_and_query):
    """GET from consumers API."""
    url = f"{API_DOMAIN}/{path_and_query}"
    return await page.evaluate(f"""
        async () => {{
            const resp = await fetch('{url}', {{
                credentials: 'include',
                headers: {{'application-id': '{APP_ID}'}}
            }});
            return await resp.json();
        }}
    """)


# ─── Auth commands ────────────────────────────────────────────────────────────

async def cmd_send(phone: str):
    """Open Firefox, navigate login page, click OTP tab, submit phone."""
    print(f"[pluxee] Sending OTP to {phone}...")
    otp_response = None

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        async def capture_sendotp(response):
            nonlocal otp_response
            if "sendOTP" in response.url:
                try:
                    otp_response = await response.json()
                except Exception:
                    pass

        page.on("response", capture_sendotp)

        await page.goto(SITE_URL + "/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        for el in await page.query_selector_all("div"):
            if (await el.text_content()).strip() == "קוד חד פעמי" and await el.is_visible():
                await el.click()
                await page.wait_for_timeout(600)
                break

        first_input = await page.query_selector("#firstInput")
        if not first_input:
            print("[pluxee] Error: could not find phone input.", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        await first_input.fill(phone)

        for btn in await page.query_selector_all("button"):
            if "שנמשיך" in (await btn.text_content()).strip() and await btn.is_visible():
                await btn.click()
                break

        deadline = time.time() + 10
        while otp_response is None and time.time() < deadline:
            await asyncio.sleep(0.3)

        if otp_response is None:
            print("[pluxee] Timeout waiting for OTP response.", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        status = otp_response.get("status", 0)
        if status == 429:
            print("[pluxee] Rate limited. Wait ~1 minute and try again.", file=sys.stderr)
            await browser.close()
            sys.exit(1)
        if status != 201:
            err = otp_response.get("error", {}).get("message", "Unknown error")
            print(f"[pluxee] Failed to send OTP (status {status}): {err}", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        data = otp_response.get("data", {})
        masked = data.get("maskedInput", phone)
        method = data.get("method", "phone")

        state = await context.storage_state()
        save_json(BROWSER_STATE_FILE, state)
        save_json(SESSION_FILE, {"phone": phone, "sent_at": time.time()})
        await browser.close()

    print(f"[pluxee] OTP sent via {method} to {masked}")
    print(f"[pluxee] Please provide the 6-digit code from your message.")
    print(f"[pluxee] Then run: python3 pluxee.py verify <code>")


async def cmd_verify(otp: str):
    """Restore browser state, get reCAPTCHA token, call authToken."""
    session = load_json(SESSION_FILE)
    if not session:
        print("[pluxee] No active session. Run 'pluxee.py send' first.", file=sys.stderr)
        sys.exit(1)

    phone = session.get("phone", DEFAULT_PHONE)
    if time.time() - session.get("sent_at", 0) > 600:
        print("[pluxee] Session expired (>10 min). Run 'pluxee.py send' again.", file=sys.stderr)
        sys.exit(1)

    browser_state = load_json(BROWSER_STATE_FILE)
    if not browser_state:
        print("[pluxee] No browser state found. Run 'pluxee.py send' first.", file=sys.stderr)
        sys.exit(1)

    print(f"[pluxee] Verifying OTP for {phone}...")

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        context = await browser.new_context(storage_state=browser_state)
        page = await context.new_page()

        await page.goto(SITE_URL + "/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        try:
            captcha_token = await page.evaluate(f"""
                () => new Promise((resolve, reject) => {{
                    grecaptcha.execute('{RECAPTCHA_SITE_KEY}', {{action: 'loginOtp'}})
                        .then(resolve).catch(reject);
                }})
            """)
        except Exception as e:
            print(f"[pluxee] Failed to get reCAPTCHA token: {e}", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        result = await page.evaluate(f"""
            async () => {{
                try {{
                    const resp = await fetch('{CAPIR_BASE_URL}/auth/authToken', {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            otpPin: '{otp}',
                            userInput1: '{phone}',
                            userInput2: '',
                            reCAPTCHAToken: '{captcha_token}',
                            trustDevice: false
                        }})
                    }});
                    return await resp.json();
                }} catch(e) {{ return {{status: 500, error: {{message: String(e)}}}}; }}
            }}
        """)

        status = result.get("status", 0)
        if status >= 400:
            err = result.get("error", {}).get("message", "Unknown error")
            print(f"[pluxee] Login failed (status {status}): {err}", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        token_data = result.get("data", {})
        full_state = await context.storage_state()
        save_json(FULL_SESSION_FILE, full_state)
        await browser.close()

    save_json(TOKEN_FILE, token_data)
    SESSION_FILE.unlink(missing_ok=True)
    BROWSER_STATE_FILE.unlink(missing_ok=True)

    jwt = token_data.get("token", token_data.get("authToken", ""))
    print(f"[pluxee] Login successful! Token saved to {TOKEN_FILE}")
    if jwt:
        print(f"[pluxee] JWT: {jwt[:60]}...")


def cmd_status():
    token_data = load_json(TOKEN_FILE)
    session = load_json(SESSION_FILE)

    if session:
        remaining = max(0, 600 - (time.time() - session.get("sent_at", 0)))
        print(f"[pluxee] Pending OTP for {session['phone']} ({int(remaining)}s remaining)")

    if not token_data:
        print("[pluxee] Not logged in. Run 'pluxee.py send' to start.")
        return

    jwt = token_data.get("token", token_data.get("authToken", ""))
    print(f"[pluxee] Logged in. Token file: {TOKEN_FILE}")
    if jwt:
        print(f"[pluxee] JWT: {jwt[:60]}...")


def cmd_logout():
    TOKEN_FILE.unlink(missing_ok=True)
    SESSION_FILE.unlink(missing_ok=True)
    BROWSER_STATE_FILE.unlink(missing_ok=True)
    FULL_SESSION_FILE.unlink(missing_ok=True)
    print("[pluxee] Logged out.")


# ─── Account commands ─────────────────────────────────────────────────────────

async def cmd_balance():
    p, browser, page = await _open_browser(require_session())
    try:
        result = await _post(page, {"type": "prx_get_budgets"})
        if result.get("code") == 0:
            for b in result.get("data", []):
                budget = float(b.get("CurrBudget", 0))
                total = float(b.get("CreatioBudget", 0))
                expires = b.get("ExpirationDate", "?")
                print(f"Balance: ₪{budget:.2f} / ₪{total:.2f}  (expires {expires})")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await browser.close()
        await p.stop()


async def cmd_whoami():
    p, browser, page = await _open_browser(require_session())
    try:
        result = await _post(page, {"type": "prx_user_info"})
        if result.get("code") == 0:
            info = result.get("data", result)
            print(f"Name: {info.get('first_name', '')} {info.get('last_name', '')}")
            print(f"Email: {info.get('email', '?')}")
            print(f"Phone: {info.get('phone', '?')}")
            print(f"Company: {info.get('company_name', '?')}")
            print(f"Card: {info.get('scard', '?')}")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await browser.close()
        await p.stop()


async def cmd_orders(count: int = 5):
    """Fetch last N transactions."""
    from datetime import datetime
    now = datetime.now()
    to_date = now.strftime("%d/%m/%Y")
    from_date = f"01/01/{now.year - 2}"

    print(f"[pluxee] Fetching last {count} orders...")
    p, browser, page = await _open_browser(require_session())
    try:
        result = await _post(page, {
            "type": "prx_user_deals",
            "from_date": from_date,
            "to_date": to_date
        })
    finally:
        await browser.close()
        await p.stop()

    if result.get("code") != 0:
        print(f"[pluxee] Error: {result}", file=sys.stderr)
        sys.exit(1)

    orders = result.get("list", [])[:count]
    if not orders:
        print("[pluxee] No orders found.")
        return

    print(f"\n{'Restaurant':<40} {'Date':<12} {'Time':<6} {'Amount':>8} {'Discount':>9} {'Charged':>8} {'Status'}")
    print("-" * 100)
    for o in orders:
        name = o.get("rest_name", "?")[:39]
        date = o.get("date", "?")
        t = o.get("time", "?")
        price = f"₪{o.get('display_price', 0):.2f}"
        discount = f"₪{o.get('discount', 0):.2f}"
        charged = f"₪{o.get('price', 0):.2f}"
        status = "✗ cancelled" if o.get("is_active") == 0 else "✓"
        print(f"{name:<40} {date:<12} {t:<6} {price:>8} {discount:>9} {charged:>8}  {status}")

    print(f"\nShowing {len(orders)} of {result.get('head', {}).get('count', '?')} total.")


# ─── Restaurant exploration ───────────────────────────────────────────────────

async def cmd_restaurants(order_type: int = 1, limit: int = 20):
    """List available restaurants. order_type: 1=pickup, 2=delivery."""
    type_name = "pickup" if order_type == 1 else "delivery"
    print(f"[pluxee] Fetching {type_name} restaurants...")

    p, browser, page = await _open_browser(require_session())
    try:
        # Step 1: get scan hash
        scan = await _post(page, {"type": "rest_scan", "get_hash": True, "order_type": order_type})
        if not scan.get("hash"):
            print(f"[pluxee] Failed to get restaurant hash: {scan}", file=sys.stderr)
            sys.exit(1)

        h = scan["hash"]

        # Step 2: fetch restaurant list
        rests_data = await _get(page, f"rest_scan.py?hash={h}&lang=he")
    finally:
        await browser.close()
        await p.stop()

    rests = rests_data.get("list", [])
    if not rests:
        print("[pluxee] No restaurants found.")
        return

    open_rests = [r for r in rests if r.get("is_open") == 1]
    closed_rests = [r for r in rests if r.get("is_open") != 1]

    print(f"\nOpen now ({len(open_rests)} restaurants):")
    print(f"{'ID':<10} {'Name':<45} {'City':<20} {'Dist'}")
    print("-" * 90)
    shown = 0
    for r in open_rests:
        if shown >= limit:
            break
        dist = r.get("dist")
        dist_str = f"{dist/1000:.1f}km" if isinstance(dist, (int, float)) else "?"
        name = r.get("name", "?")[:44]
        city = (r.get("City") or "")[:19]
        print(f"{r['restaurant_id']:<10} {name:<45} {city:<20} {dist_str}")
        shown += 1

    if closed_rests and shown < limit:
        print(f"\nClosed now (first {min(limit - shown, len(closed_rests))}):")
        print(f"{'ID':<10} {'Name':<45} {'City':<20} {'Dist'}")
        print("-" * 90)
        for r in closed_rests[:limit - shown]:
            dist = r.get("dist")
            dist_str = f"{dist/1000:.1f}km" if isinstance(dist, (int, float)) else "?"
            name = r.get("name", "?")[:44]
            city = (r.get("City") or "")[:19]
            print(f"{r['restaurant_id']:<10} {name:<45} {city:<20} {dist_str}")

    print(f"\nTotal: {len(rests)} restaurants ({len(open_rests)} open). Use 'menu <id>' to see a menu.")


async def cmd_menu(restaurant_id: int, order_type: int = 1):
    """Display the menu for a restaurant."""
    print(f"[pluxee] Fetching menu for restaurant {restaurant_id}...")

    p, browser, page = await _open_browser(require_session())
    try:
        # Get user info for comp_id and address_id
        userinfo = await _get(page, f"prx_user_info.py?version=1&rand_onboarding=0")
        comp_id = userinfo.get("comp_id") or userinfo.get("company_id", 0)
        addr_id = userinfo.get("default_addr_id", -1)

        # Get restaurant info
        rest_info = await _get(
            page,
            f"prx_rest_info.py?order_type={order_type}&restaurant_id={restaurant_id}&lang=he"
        )

        # Get menu tree (element_type_deep=15 returns full nested structure)
        menu_tree = await _get(
            page,
            f"rest_menu_tree.py?restaurant_id={restaurant_id}&comp_id={comp_id}"
            f"&order_type={order_type}&element_type_deep=15&lang=he&address_id={addr_id}"
        )
    finally:
        await browser.close()
        await p.stop()

    # Print restaurant header
    name = rest_info.get("name", f"Restaurant #{restaurant_id}")
    city = rest_info.get("address", "")
    is_open = rest_info.get("is_open", 0)
    status = "OPEN" if is_open else "CLOSED"
    rate = rest_info.get("rate", 0)
    print(f"\n{'='*60}")
    print(f"{name}  [{status}]  ★{rate:.1f}")
    print(f"{city}")
    print(f"{'='*60}")

    # Parse menu tree — structure: {11: [{...12: [{...13: [items]}]}]}
    def extract_categories(node):
        cats = []
        if not isinstance(node, dict):
            return cats
        for key, val in node.items():
            if not key.isdigit() or not isinstance(val, list):
                continue
            etype = int(key)
            for item in val:
                if etype == 11:  # top-level category
                    cats.append({
                        "name": item.get("name", ""),
                        "subcats": extract_categories(item)
                    })
                elif etype == 12:  # subcategory
                    cats.append({
                        "name": item.get("name", ""),
                        "items": extract_items(item)
                    })
        return cats

    def extract_items(node):
        items = []
        if not isinstance(node, dict):
            return items
        for key, val in node.items():
            if not key.isdigit() or not isinstance(val, list):
                continue
            etype = int(key)
            if etype == 13:  # menu item
                for item in val:
                    name = item.get("name", "")
                    # Skip internal placeholder items
                    if name.lower() in ("daily distribution", "") and item.get("price", 0) == 0:
                        continue
                    items.append({
                        "id": item.get("element_id"),
                        "name": name,
                        "price": item.get("price", 0),
                        "desc": item.get("description", ""),
                    })
        return items

    def print_category(cat, depth=0):
        indent = "  " * depth
        print(f"\n{indent}[{cat['name']}]")
        for subcat in cat.get("subcats", []):
            print_category(subcat, depth + 1)
        for item in cat.get("items", []):
            price_str = f" ₪{item['price']}" if item["price"] > 0 else ""
            desc = f"  — {item['desc'][:60]}" if item.get("desc") else ""
            print(f"{indent}  • {item['name']}{price_str}{desc}")

    cats = extract_categories(menu_tree)
    if not cats:
        print("[pluxee] Menu not available (restaurant may be closed or not registered).")
        return

    for cat in cats:
        print_category(cat)

    print()


# ─── Morning ping ────────────────────────────────────────────────────────────

# Load from config (see ~/.pluxee/config.json)
FAVORITES = CONFIG.get("favorites", {})
WATCHED_CATEGORIES = {int(k): v for k, v in CONFIG.get("watched_categories", {}).items()}
CATEGORY_MAX_DIST = CONFIG.get("category_max_dist", 10000)


async def cmd_morning_ping(order_type: int = 1):
    """Fetch restaurants, filter favorites, print Telegram-ready morning summary."""
    p, browser, page = await _open_browser(require_session())
    try:
        scan = await _post(page, {"type": "rest_scan", "get_hash": True, "order_type": order_type})
        if not scan.get("hash"):
            print("❌ Could not fetch restaurants.", file=sys.stderr)
            sys.exit(1)
        rests_data = await _get(page, f"rest_scan.py?hash={scan['hash']}&lang=he")
    finally:
        await browser.close()
        await p.stop()

    rests = rests_data.get("list", [])

    # Match favorites
    open_favs, closed_favs, other_open = [], [], []
    seen_fav_names = set()
    for r in rests:
        name_lower = r.get("name", "").lower()
        display = None
        for key, label in FAVORITES.items():
            if key.lower() in name_lower:
                display = label
                break
        dist = r.get("dist")
        dist_str = f"{dist/1000:.1f}km" if isinstance(dist, (int, float)) else ""
        entry = {"name": r.get("name", "?"), "display": display or r.get("name", "?"),
                 "id": r["restaurant_id"], "dist": dist_str, "is_fav": display is not None}
        if display:
            if display in seen_fav_names:
                continue
            seen_fav_names.add(display)
            if r.get("is_open") == 1:
                open_favs.append(entry)
            else:
                closed_favs.append(entry)
        elif r.get("is_open") == 1:
            other_open.append(entry)

    # Match watched categories (nearby only)
    cat_open = {}   # category label → list of open restaurants
    cat_closed = {} # category label → list of closed restaurants
    for r in rests:
        dist = r.get("dist", 999999)
        if dist > CATEGORY_MAX_DIST:
            continue
        ftypes = r.get("food_types", [])
        if not isinstance(ftypes, list):
            ftypes = []
        for code, label in WATCHED_CATEGORIES.items():
            if code in ftypes:
                dist_str = f"{dist/1000:.1f}km" if isinstance(dist, (int, float)) else ""
                entry = {"name": r.get("name", "?"), "id": r["restaurant_id"], "dist": dist_str}
                if r.get("is_open") == 1:
                    cat_open.setdefault(label, []).append(entry)
                else:
                    cat_closed.setdefault(label, []).append(entry)

    from datetime import date
    today = date.today().strftime("%A, %d %b")
    lines = [f"🍽️ *Pluxee Morning Ping* — {today}"]

    if open_favs:
        lines.append("\n⭐ *Favorites open now:*")
        for r in open_favs:
            lines.append(f"  • {r['display']} ({r['dist']})")
    if closed_favs:
        lines.append("\n❌ *Favorites closed:*")
        for r in closed_favs:
            lines.append(f"  • {r['display']}")

    for label in WATCHED_CATEGORIES.values():
        opened = cat_open.get(label, [])
        closed = cat_closed.get(label, [])
        if opened:
            lines.append(f"\n{label} *open:*")
            for r in opened:
                fav_mark = " ⭐" if any(r["name"].lower().find(k) >= 0 for k in FAVORITES) else ""
                lines.append(f"  • {r['name']} ({r['dist']}){fav_mark}")
        if closed:
            lines.append(f"\n{label} *closed:*")
            for r in closed:
                lines.append(f"  • {r['name']}")

    if other_open:
        lines.append(f"\n🟢 *Other open nearby ({len(other_open)}):*")
        for r in other_open[:5]:
            lines.append(f"  • {r['name']} ({r['dist']})")
        if len(other_open) > 5:
            lines.append(f"  _...and {len(other_open) - 5} more_")

    if not open_favs and not other_open and not cat_open:
        lines.append("\n😴 No restaurants open right now.")

    print("\n".join(lines))


# ─── Main ─────────────────────────────────────────────────────────────────────

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
            if args[i] == "--phone" and i + 1 < len(args):
                cfg["phone"] = args[i + 1]; i += 2
            else:
                i += 1
        _save_config(cfg)
        print(f"[pluxee] Config saved to {CONFIG_FILE}")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return

    elif cmd == "send":
        phone = DEFAULT_PHONE
        i = 1
        while i < len(args):
            if args[i] in ("--phone", "-p") and i + 1 < len(args):
                phone = args[i + 1]; i += 2
            else:
                i += 1
        if not phone:
            print("[pluxee] No phone configured. Run: pluxee.py configure --phone 05XXXXXXXX", file=sys.stderr)
            sys.exit(1)
        await cmd_send(phone)

    elif cmd == "verify":
        if len(args) < 2:
            print("Usage: pluxee.py verify <otp>", file=sys.stderr)
            sys.exit(1)
        await cmd_verify(args[1].strip())

    elif cmd == "status":
        cmd_status()

    elif cmd == "balance":
        await cmd_balance()

    elif cmd == "whoami":
        await cmd_whoami()

    elif cmd == "orders":
        count = 5
        i = 1
        while i < len(args):
            if args[i] in ("--count", "-n") and i + 1 < len(args):
                count = int(args[i + 1]); i += 2
            else:
                i += 1
        await cmd_orders(count)

    elif cmd == "restaurants":
        order_type = 1
        limit = 20
        i = 1
        while i < len(args):
            if args[i] in ("--type", "-t") and i + 1 < len(args):
                order_type = 2 if args[i + 1].startswith("d") else 1; i += 2
            elif args[i] in ("--limit", "-l") and i + 1 < len(args):
                limit = int(args[i + 1]); i += 2
            else:
                i += 1
        await cmd_restaurants(order_type, limit)

    elif cmd == "menu":
        if len(args) < 2:
            print("Usage: pluxee.py menu <restaurant_id> [--type pickup|delivery]", file=sys.stderr)
            sys.exit(1)
        restaurant_id = int(args[1])
        order_type = 1
        i = 2
        while i < len(args):
            if args[i] in ("--type", "-t") and i + 1 < len(args):
                order_type = 2 if args[i + 1].startswith("d") else 1; i += 2
            else:
                i += 1
        await cmd_menu(restaurant_id, order_type)

    elif cmd == "morning_ping":
        order_type = 1
        i = 1
        while i < len(args):
            if args[i] in ("--type", "-t") and i + 1 < len(args):
                order_type = 2 if args[i + 1].startswith("d") else 1; i += 2
            else:
                i += 1
        await cmd_morning_ping(order_type)

    elif cmd == "logout":
        cmd_logout()

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        usage_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
