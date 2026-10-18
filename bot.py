"""Forex news bot: ForexFactory kalendář -> Claude -> Discord.

Použití:
    python bot.py --mode morning          # události 08:00–20:00 dnes
    python bot.py --mode evening          # události 20:00 dnes – 08:00 zítra
    python bot.py --mode auto             # podle plánovaného času (GitHub Actions)
    python bot.py --mode morning --dry-run --no-ai   # jen vypíše, nic neposílá
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
import certifi

FEED_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",  # často 404, nevadí
]
IMPACT_RANK = {"Holiday": 0, "Low": 1, "Medium": 2, "High": 3}
IMPACT_ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪"}

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Prague"))
MIN_IMPACT = os.getenv("MIN_IMPACT", "Medium")
CURRENCIES = {c.strip().upper() for c in os.getenv("CURRENCIES", "").split(",") if c.strip()}
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
SEND_EMPTY = os.getenv("SEND_EMPTY", "true").lower() == "true"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

SYSTEM_PROMPT = """Jsi asistent forex tradera. Dostaneš seznam nadcházejících ekonomických \
událostí z ForexFactory (časy už jsou v místním čase tradera). Napiš stručný přehled \
v češtině pro Discord (Markdown, žádné tabulky).

Struktura:
1. Jedna věta: jak rušné období to je a kterých měn se týká nejvíc.
2. Klíčové události (🔴 High): čas, měna, název, prognóza vs. předchozí hodnota, \
1–2 věty proč je důležitá a co by znamenalo překvapení nahoru/dolů pro měnu. \
Zmiň nejvíc ovlivněné páry (např. EUR/USD, XAU/USD).
3. Ostatní (🟠 Medium): krátký seznam, jeden řádek na událost.
4. Upozornění na shluky událostí (víc zpráv do 30 minut) a projevy centrálních bankéřů.

Buď věcný, nedávej obchodní doporučení (nekupuj/neprodávej). Max. 3500 znaků."""


def fetch_events():
    events = []
    for url in FEED_URLS:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 forex-news-bot"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                events.extend(json.load(resp))
        except urllib.error.HTTPError as e:
            if url == FEED_URLS[0]:
                raise
            print(f"Přeskakuji {url}: HTTP {e.code}", file=sys.stderr)
    # Deduplikace (feedy se mohou překrývat)
    seen, unique = set(), []
    for ev in events:
        key = (ev["title"], ev["country"], ev["date"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return unique


def session_window(mode, now):
    today = now.astimezone(TZ).date()
    at = lambda d, h: datetime(d.year, d.month, d.day, h, tzinfo=TZ)
    if mode == "morning":
        return at(today, 8), at(today, 20), "☀️ Denní seance"
    return at(today, 20), at(today + timedelta(days=1), 8), "🌙 Noční seance"


def filter_events(events, start, end):
    out = []
    for ev in events:
        when = datetime.fromisoformat(ev["date"]).astimezone(TZ)
        if not (start <= when < end):
            continue
        if IMPACT_RANK.get(ev["impact"], 0) < IMPACT_RANK[MIN_IMPACT]:
            continue
        if CURRENCIES and ev["country"].upper() not in CURRENCIES:
            continue
        out.append({**ev, "local": when})
    return sorted(out, key=lambda e: e["local"])


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


def ai_summary(events, label, start, end):
    client = anthropic.Anthropic()
    payload = [
        {
            "cas": f"{e['local']:%a %d.%m. %H:%M}",
            "mena": e["country"],
            "udalost": e["title"],
            "dopad": e["impact"],
            "prognoza": e.get("forecast") or None,
            "predchozi": e.get("previous") or None,
        }
        for e in events
    ]
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"{label}: {start:%d.%m. %H:%M} – {end:%d.%m. %H:%M}\n\n"
                       + json.dumps(payload, ensure_ascii=False, indent=1),
        }],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude odmítl požadavek")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def send_discord(title, description):
    webhook = os.environ["DISCORD_WEBHOOK_URL"]
    # Embed description má limit 4096 znaků -> případně rozdělit
    chunks = [description[i:i + 4000] for i in range(0, len(description), 4000)] or ["—"]
    for i, chunk in enumerate(chunks):
        embed = {"description": chunk, "color": 0xF1C40F}
        if i == 0:
            embed["title"] = title
        if i == len(chunks) - 1:
            embed["footer"] = {"text": "Zdroj: ForexFactory • časy: " + str(TZ)}
        body = json.dumps({"username": "Forex News Bot", "embeds": [embed]}).encode()
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "forex-news-bot"},
        )
        urllib.request.urlopen(req, timeout=30, context=SSL_CTX).close()


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["morning", "evening", "auto"], default="auto")
    p.add_argument("--dry-run", action="store_true", help="nic neposílat, jen vypsat")
    p.add_argument("--no-ai", action="store_true", help="bez Claude, jen seznam událostí")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    mode = args.mode if args.mode != "auto" else resolve_auto_mode(now)
    if mode is None:
        print("Teď není 8:00 ani 20:00 místního času – končím.")
        return

    start, end, label = session_window(mode, now)
    events = filter_events(fetch_events(), start, end)
    title = f"{label} • {start:%d.%m. %H:%M} – {end:%d.%m. %H:%M}"
    print(f"{title}: {len(events)} událostí")

    if not events:
        if not SEND_EMPTY:
            return
        text = "Žádné významné události v tomto období. Klidné obchodování! ✅"
    elif args.no_ai:
        text = plain_list(events)
    else:
        try:
            text = ai_summary(events, label, start, end)
        except Exception as e:  # upozornění nesmí kvůli AI vypadnout
            print(f"AI selhalo ({e}), posílám prostý seznam", file=sys.stderr)
            text = plain_list(events)

    if args.dry_run:
        print("\n" + text)
    else:
        send_discord(title, text)
        print("Odesláno na Discord.")


if __name__ == "__main__":
    main()
