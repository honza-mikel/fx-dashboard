"""Forex news bot: ForexFactory kalendář -> Claude -> Discord.

Použití:
    python bot.py --mode morning          # přehled 08:00–20:00 dnes
    python bot.py --mode evening          # přehled 20:00 dnes – 08:00 zítra
    python bot.py --mode auto             # podle plánovaného času (GitHub Actions)
    python bot.py --mode alert            # upozornění ~15 min před High Impact zprávou
    python bot.py --mode alert --dry-run --no-ai --now 2026-10-21T07:45
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import certifi

FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",  # často 404, nevadí
]
IMPACT_RANK = {"Holiday": 0, "Low": 1, "Medium": 2, "High": 3}
IMPACT_ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪"}

# --- Nastavení (lze přepsat proměnnými na GitHubu: Settings -> Variables) ---
TZ = ZoneInfo(os.getenv("TIMEZONE") or "Europe/Prague")
CURRENCIES = {c.strip().upper() for c in (os.getenv("CURRENCIES") or "AUD,CAD,USD").split(",") if c.strip()}
MIN_IMPACT = os.getenv("MIN_IMPACT") or "Medium"          # pro ranní/večerní přehled
ALERT_IMPACT = os.getenv("ALERT_IMPACT") or "High"        # pro upozornění před zprávou
ALERT_WINDOW_MIN = int(os.getenv("ALERT_WINDOW_MIN") or 20)  # upozorni, když zbývá <= N minut
ALERT_PING = os.getenv("ALERT_PING") or ""                 # např. @here pro notifikaci na mobil
MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"
SEND_EMPTY = (os.getenv("SEND_EMPTY") or "true").lower() == "true"

COLOR_MORNING = 0xF1C40F  # žlutá
COLOR_EVENING = 0x9B59B6  # fialová
COLOR_ALERT = 0xE74C3C    # červená

STATE_FILE = Path(__file__).parent / "state" / "alerted.json"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

TRADER_PROFILE = """Trader obchoduje téměř výhradně AUD/CAD a připravuje se na XAU/USD (zlato). \
Sleduje zprávy pro AUD, CAD a USD. USD je důležitý hlavně kvůli zlatu (silný dolar = tlak na zlato) \
a kvůli celkové náladě na trhu (risk-on/risk-off), která hýbe i AUD/CAD. \
AUD/CAD je pár dvou komoditních měn: AUD citlivý na Čínu, kovy a risk sentiment, \
CAD na ropu. Zlato reaguje na USD, výnosy dluhopisů, očekávání sazeb Fedu a risk-off nálady."""

SESSION_PROMPT = TRADER_PROFILE + """

Dostaneš seznam nadcházejících ekonomických událostí z ForexFactory (časy jsou v místním čase \
tradera). Napiš přehled v češtině pro Discord (Markdown, žádné tabulky, žádný nadpis).

Struktura:
1. **Shrnutí** – 1–2 věty: jak rušné období to je a jestli čekat pohyb na AUD/CAD a XAU/USD.
2. **🔴 High Impact** – to nejdůležitější, věnuj tomu nejvíc prostoru. U každé události: čas, \
měna, název, prognóza vs. předchozí. Pak 2–3 věty: proč je důležitá a co by znamenal výsledek \
nad/pod prognózou konkrétně pro AUD/CAD a pro XAU/USD (směr pohybu).
3. **🟠 Medium Impact** – méně důležité, jen jeden řádek na událost (čas, měna, název, \
případně krátká poznámka, pokud může AUD/CAD nebo zlato výrazněji pohnout).
4. **⚠️ Pozor** – shluky zpráv (víc událostí do 30 minut), projevy guvernérů RBA, BoC a Fedu, \
střet AUD a CAD zpráv ve stejný den. Pokud nic takového není, sekci vynech.

Když nejsou žádné High Impact události, napiš to hned v shrnutí. Buď věcný a stručný, \
nedávej obchodní doporučení (nekupuj/neprodávej). Max. 3000 znaků."""

ALERT_PROMPT = TRADER_PROFILE + """

Za pár minut vyjde High Impact zpráva (případně víc zpráv najednou). Napiš v češtině \
2–4 krátké věty pro Discord: co zpráva měří, co by výsledek nad/pod prognózou znamenal pro \
AUD/CAD a pro XAU/USD, a upozorni na možnou volatilitu a rozšířený spread. \
Žádné obchodní doporučení, žádný nadpis, max. 600 znaků."""


# --- Data ---

def fetch_url(url, attempts=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 forex-news-bot"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # 429 = feed omezuje počet požadavků, chvíli počkej a zkus znovu
            if e.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(30)


def fetch_events(include_next_week=True):
    if os.getenv("FF_FEED_FILE"):  # jen pro lokální testy
        return json.loads(Path(os.environ["FF_FEED_FILE"]).read_text())
    events = fetch_url(FEED_URLS[0])
    if include_next_week:
        try:
            events += fetch_url(FEED_URLS[1], attempts=1)
        except urllib.error.HTTPError as e:
            print(f"Přeskakuji příští týden: HTTP {e.code}", file=sys.stderr)
    seen, unique = set(), []
    for ev in events:
        key = event_key(ev)
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def event_key(ev):
    return f"{ev['date']}|{ev['country']}|{ev['title']}"


def filter_events(events, start, end, min_impact):
    out = []
    for ev in events:
        when = datetime.fromisoformat(ev["date"]).astimezone(TZ)
        if not (start <= when < end):
            continue
        if IMPACT_RANK.get(ev["impact"], 0) < IMPACT_RANK[min_impact]:
            continue
        if CURRENCIES and ev["country"].upper() not in CURRENCIES:
            continue
        out.append({**ev, "local": when})
    return sorted(out, key=lambda e: (e["local"], -IMPACT_RANK[e["impact"]]))


def plain_list(events):
    lines = []
    for ev in events:
        extra = []
        if ev.get("forecast"):
            extra.append(f"prognóza {ev['forecast']}")
        if ev.get("previous"):
            extra.append(f"předchozí {ev['previous']}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(
            f"{IMPACT_ICON.get(ev['impact'], '')} **{ev['local']:%H:%M}** "
            f"`{ev['country']}` {ev['title']}{suffix}"
        )
    return "\n".join(lines)


def events_payload(events):
    return json.dumps([
        {
            "cas": f"{e['local']:%a %d.%m. %H:%M}",
            "mena": e["country"],
            "udalost": e["title"],
            "dopad": e["impact"],
            "prognoza": e.get("forecast") or None,
            "predchozi": e.get("previous") or None,
        }
        for e in events
    ], ensure_ascii=False, indent=1)


# --- Claude ---

def ask_claude(system, user, effort):
    client = anthropic.Anthropic()
    extra = {}
    if not MODEL.startswith("claude-haiku"):  # Haiku nemá serverový fallback
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": effort},
        system=system,
        messages=[{"role": "user", "content": user}],
        **extra,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude odmítl požadavek")
    return "".join(b.text for b in response.content if b.type == "text").strip()


# --- Discord ---

def send_discord(title, description, color, content=""):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    chunks = [description[i:i + 4000] for i in range(0, len(description), 4000)] or ["—"]
    for i, chunk in enumerate(chunks):
        embed = {"description": chunk, "color": color}
        if i == 0:
            embed["title"] = title
        if i == len(chunks) - 1:
            embed["footer"] = {"text": "Zdroj: ForexFactory • časy: " + str(TZ)}
        body = {"username": "Forex News Bot", "embeds": [embed]}
        if content and i == 0:
            body["content"] = content
        req = urllib.request.Request(
            webhook, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "forex-news-bot"},
        )
        urllib.request.urlopen(req, timeout=30, context=SSL_CTX).close()


# --- Ranní / večerní přehled ---

def session_window(mode, now):
    today = now.astimezone(TZ).date()
    at = lambda d, h: datetime(d.year, d.month, d.day, h, tzinfo=TZ)
    if mode == "morning":
        return at(today, 8), at(today, 20), "☀️ Denní seance", COLOR_MORNING
    return at(today, 20), at(today + timedelta(days=1), 8), "🌙 Noční seance", COLOR_EVENING


def resolve_auto_mode(now):
    """GitHub Actions spouští cron v UTC. Kvůli letnímu/zimnímu času máme 2 crony
    pro každé upozornění a podle *plánovaného* času (ne skutečného, Actions mívá
    zpoždění) vybereme ten, který odpovídá 8:00 / 20:00 místního času."""
    schedule = os.getenv("SCHEDULE", "")  # např. "0 6 * * *"
    if not schedule:
        hour = now.astimezone(TZ).hour
    else:
        minute, utc_hour = int(schedule.split()[0]), int(schedule.split()[1])
        planned = now.replace(hour=utc_hour, minute=minute, second=0, microsecond=0)
        hour = planned.astimezone(TZ).hour
    return {8: "morning", 20: "evening"}.get(hour)


def run_session(mode, now, args):
    start, end, label, color = session_window(mode, now)
    events = filter_events(fetch_events(), start, end, MIN_IMPACT)
    title = f"{label} • {start:%d.%m. %H:%M} – {end:%d.%m. %H:%M}"
    print(f"{title}: {len(events)} událostí")

    if not events:
        if not SEND_EMPTY:
            return
        text = "Žádné významné zprávy pro " + ", ".join(sorted(CURRENCIES)) + ". Klidné obchodování! ✅"
    elif args.no_ai:
        text = plain_list(events)
    else:
        try:
            user = f"{label}: {start:%d.%m. %H:%M} – {end:%d.%m. %H:%M}\n\n{events_payload(events)}"
            text = ask_claude(SESSION_PROMPT, user, effort="medium")
        except Exception as e:  # upozornění nesmí kvůli AI vypadnout
            print(f"AI selhalo ({e}), posílám prostý seznam", file=sys.stderr)
            text = plain_list(events)

    if args.dry_run:
        print(f"[barva {color:#08x}]\n{text}")
    else:
        send_discord(title, text, color)
        print("Odesláno na Discord.")


# --- Upozornění před High Impact zprávou ---

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state, now):
    # Záznamy starší než 3 dny už nejsou potřeba
    cutoff = now - timedelta(days=3)
    state = {k: v for k, v in state.items() if datetime.fromisoformat(v) > cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def run_alert(now, args):
    """Spouští se každých 10 minut. Pošle upozornění na High Impact zprávy, které vyjdou
    během příštích ALERT_WINDOW_MIN minut a ještě o nich nepřišlo upozornění. Díky tomu
    zpožděný nebo vynechaný běh GitHubu dožene ten další."""
    upcoming = filter_events(fetch_events(include_next_week=False), now, now + timedelta(minutes=ALERT_WINDOW_MIN), ALERT_IMPACT)
    state = load_state()
    pending = [e for e in upcoming if event_key(e) not in state]
    if not pending:
        print("Žádná blížící se High Impact zpráva.")
        return

    # Zprávy vycházející ve stejný čas (např. NFP + nezaměstnanost) = jedno upozornění
    groups = {}
    for ev in pending:
        groups.setdefault(ev["local"], []).append(ev)

    for when, group in sorted(groups.items()):
        minutes = max(1, round((when - now).total_seconds() / 60))
        names = ", ".join(f"{e['country']} {e['title']}" for e in group)
        title = f"⏰ Za {minutes} min ({when:%H:%M}): {names}"[:256]
        text = plain_list(group)
        if not args.no_ai:
            try:
                comment = ask_claude(ALERT_PROMPT, events_payload(group), effort="low")
                text += "\n\n" + comment
            except Exception as e:
                print(f"AI selhalo ({e}), posílám bez komentáře", file=sys.stderr)

        if args.dry_run:
            print(f"[barva {COLOR_ALERT:#08x}] {title}\n{text}\n")
        else:
            send_discord(title, text, COLOR_ALERT, content=ALERT_PING)
            for ev in group:
                state[event_key(ev)] = ev["date"]
            print(f"Odesláno upozornění: {names}")

    if not args.dry_run:
        save_state(state, now)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["morning", "evening", "auto", "alert"], default="auto")
    p.add_argument("--dry-run", action="store_true", help="nic neposílat, jen vypsat")
    p.add_argument("--no-ai", action="store_true", help="bez Claude, jen seznam událostí")
    p.add_argument("--now", help="simulovaný čas pro test, např. 2026-10-21T07:45")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now).replace(tzinfo=TZ).astimezone(timezone.utc)

    if args.mode == "alert":
        run_alert(now, args)
        return

    mode = args.mode if args.mode != "auto" else resolve_auto_mode(now)
    if mode is None:
        print("Teď není 8:00 ani 20:00 místního času – končím.")
        return
    run_session(mode, now, args)


if __name__ == "__main__":
    main()
