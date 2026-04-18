---
name: cibus-pluxee
description: Log in and interact with the Pluxee (Cibus) consumer account at consumers.pluxee.co.il. Use when the user wants to browse restaurants, check Pluxee balance, view orders, see a restaurant menu, or authenticate with Pluxee. Triggers on: "pluxee", "cibus", "login to pluxee", "check cibus balance", "pluxee balance", "what's for lunch", "restaurants near work", "show me restaurants", "pluxee menu", "כניסה לסיבוס", "יתרה פלאקסי", "מסעדות", "תפריט".
---

# Cibus Pluxee

**IMPORTANT: Use the `exec` tool to run shell commands. Do NOT use `sessions_spawn` — this is a local script, not a spawnable agent.**

Manage the user's Pluxee (Cibus) corporate meal account.

## Setup

Before first use, ask the user for their phone number registered with Pluxee.

### Prerequisites
- Python 3.8+
- `playwright` with Firefox (`playwright install firefox`)
- `requests` library

## Commands (run with `exec` tool)

Resolve the script path relative to this skill directory:

```bash
SCRIPT="python3 <skill_dir>/scripts/pluxee.py"
```

### Login (two-step OTP)
```bash
$SCRIPT send --phone <user_phone>    # sends OTP SMS
$SCRIPT verify <6-digit-code>        # verifies OTP, saves session
$SCRIPT status                       # check if logged in
$SCRIPT logout
```

### Account info
```bash
$SCRIPT balance                      # ₪ remaining this month
$SCRIPT whoami                       # name, email, company, card
$SCRIPT orders                       # last 5 orders
$SCRIPT orders --count 10            # last 10 orders
```

### Browse food
```bash
$SCRIPT restaurants                        # pickup restaurants near work (default)
$SCRIPT restaurants --type delivery        # delivery restaurants
$SCRIPT restaurants --limit 30             # show more
$SCRIPT menu <restaurant_id>               # show full menu for a restaurant
$SCRIPT menu <restaurant_id> --type delivery
```

### Morning summary
```bash
$SCRIPT morning_ping                       # Telegram-formatted summary of favorite restaurants
```

## Telegram conversation flow

### If the user is NOT logged in:
1. Run `$SCRIPT status` to confirm
2. Ask the user for their Pluxee phone number
3. Run `$SCRIPT send --phone <number>` to send OTP
4. Tell the user: "I've sent a 6-digit code to your phone. What's the code?"
5. When user replies with the code, run `$SCRIPT verify <code>`
6. Confirm success, then proceed with their original request

### "What's for lunch?" / "Show me restaurants"
1. Run `$SCRIPT restaurants` to show pickup restaurants near work
2. Present the **open** restaurants as a numbered list with name and distance
3. Ask: "Which one do you want to see the menu for?"
4. When user picks one (by number or name), run `$SCRIPT menu <id>`
5. Present the menu categories and items clearly

### "Show me the menu for [restaurant name]"
- Find the restaurant_id from the `restaurants` output (or remember it from context)
- Run `$SCRIPT menu <restaurant_id>`
- Present categories and items

### "What's my balance?" / "How much do I have left?"
- Run `$SCRIPT balance`
- Report: "You have ₪X.XX left this month (out of ₪Y.YY, expires DD/MM/YYYY)"

### "What did I order recently?"
- Run `$SCRIPT orders`
- Summarize the last few orders

## Formatting for Telegram
- Use numbered lists for restaurant choices so user can reply "3" to pick the third option
- Keep responses concise — Telegram messages work best under 4096 chars
- Group menu items by category, show price next to each item
- For Hebrew restaurant names, show them as-is

## Configuration

Edit the top of `scripts/pluxee.py` to customize:
- `DEFAULT_PHONE` — your Pluxee phone number (or always pass `--phone`)
- `FAVORITES` — dict of name substrings to watch in morning_ping
- `WATCHED_CATEGORIES` — food type codes to highlight (e.g. `20011` = אוכל ביתי)
- `CATEGORY_MAX_DIST` — max distance in meters for category filtering (default: 10km)

## Notes
- Default order type: **pickup** (user collects from the restaurant)
- Restaurant list is based on the user's registered work address
- Session token valid ~24 hours — don't re-login unless `status` shows not logged in
- OTP expires after 10 minutes; if expired, run `send` again
- Rate limited (429): wait ~1 minute before retrying OTP
- Firefox headless is required — Chromium fails reCAPTCHA v3
- Each browser call takes ~8 seconds; chain commands to avoid unnecessary launches
