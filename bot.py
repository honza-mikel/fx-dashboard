"""Forex news bot: ForexFactory kalendář -> Claude -> Discord.

Použití:
    python bot.py --mode morning          # odesílá se v 06:00, přehled 06:00–22:00 dnes
    python bot.py --mode evening          # odesílá se ve 20:00, přehled 22:00 dnes – 06:00 zítra
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

# Seance (místní čas). Denní 06:00–22:00, noční 22:00–06:00.
DAY_START = 6
NIGHT_START = 22
MORNING_SEND_HOUR = 6    # kdy se posílá přehled denní seance
EVENING_SEND_HOUR = 20   # kdy se posílá přehled noční seance
# Při změně časů odesílání uprav i řádky cron v .github/workflows/notify.yml

# Dny, kdy se přehled posílá automaticky (0 = pondělí ... 6 = neděle). Přes víkend zprávy nejsou:
# ranní přehled po–pá, večerní ne–čt (v pátek večer a v sobotu nic, první v neděli ve 20:00).
MORNING_DAYS = {0, 1, 2, 3, 4}
EVENING_DAYS = {6, 0, 1, 2, 3}

COLOR_MORNING = 0x1F5AA6  # tmavší modrá
COLOR_EVENING = 0x1F5AA6  # tmavší modrá
COLOR_ALERT = 0xE74C3C    # červená

TITLE_PREFIX = ""  # při testu se simulovaným časem "🧪 TEST • "
STATE_FILE = Path(__file__).parent / "state" / "alerted.json"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

TRADER_PROFILE = """Trader obchoduje téměř výhradně AUD/CAD a připravuje se na XAU/USD (zlato). \
Sleduje zprávy pro AUD, CAD a USD. USD je důležitý hlavně kvůli zlatu (silný dolar = tlak na zlato) \
a kvůli celkové náladě na trhu (risk-on/risk-off), která hýbe i AUD/CAD. \
AUD/CAD je pár dvou komoditních měn: AUD citlivý na Čínu, kovy a risk sentiment, \
CAD na ropu. Zlato reaguje na USD, výnosy dluhopisů, očekávání sazeb Fedu a risk-off nálady."""

UP = "⬆️ 🟩"    # pár roste (zelená)
DOWN = "⬇️ 🟥"  # pár klesá (červená)

IMPACT_RULES = f"""
Jak určit směr ({UP} = pár roste, {DOWN} = pár klesá):
- Lepší data pro AUD (vyšší růst, inflace, zaměstnanost, jestřábí RBA) = silnější AUD = AUD/CAD {UP}.
- Lepší data pro CAD (totéž pro Kanadu, jestřábí BoC) = silnější CAD = AUD/CAD {DOWN}.
- Lepší data pro USD (vyšší CPI, NFP, jestřábí Fed) = silnější USD = XAU/USD {DOWN} a AUD/CAD mírně {DOWN} \
(AUD je na dolar citlivější než CAD).
- U ukazatelů, kde je nižší hodnota lepší (míra nezaměstnanosti, žádosti o podporu), je směr opačný.
- XAU/USD uváděj jen u USD zpráv.
- Používej přesně tyto značky: {UP} pro růst a {DOWN} pro pokles. Žádné jiné šipky."""

IMPACT_BLOCK = f"""> **Vyšší než očekávaný:** AUD/CAD {DOWN} · XAU/USD {DOWN}
> **Nižší než očekávaný:** AUD/CAD {UP} · XAU/USD {UP}"""

IMPACT_BLOCK_NOTE = """(Ukázka je pro USD zprávu typu CPI nebo NFP; u jiných zpráv urči směr podle pravidel výše. \
XAU/USD vynech, pokud nejde o USD zprávu.)

U projevů, zasedání a tiskových konferencí bez Forecastu (např. projev guvernéra RBA, BoC \
nebo předsedy Fedu) napiš v bloku místo „Vyšší než očekávaný“ **Jestřábí tón** a místo \
„Nižší než očekávaný“ **Holubičí tón**. Odrážky nad blokem pak budou (každá max. jeden řádek):
- Jestřábí tón: co by znamenal (např. náznak vyšších nebo déle držených sazeb).
- Holubičí tón: co by znamenal (např. náznak snižování sazeb, obavy o růst).
- Trh sleduje: na co se u projevu zaměřit (inflace, příští zasedání, výhled sazeb). Blok je vždy samostatný \
odstavec s prázdným řádkem nad i pod a každý řádek začíná znakem >."""

SESSION_PROMPT = TRADER_PROFILE + IMPACT_RULES + f"""

Dostaneš seznam nadcházejících ekonomických událostí z ForexFactory (časy jsou v místním čase \
tradera). Napiš přehled v češtině pro Discord. Používej jen Markdown pro Discord (tučné písmo, \
odrážky -, citace >), žádné tabulky ani nadpisy s #. Hodnoty označuj vždy anglicky Forecast a Previous.

Formát:

**Shrnutí:** 1–2 věty, jak rušné období to je a jestli čekat pohyb na AUD/CAD a XAU/USD.

Pak každou 🔴 High událost. Zprávy stejné měny ve stejný čas (např. CPI m/m + Median + Trimmed) \
spoj do jednoho bloku, ale každou z nich napiš jako samostatnou odrážku:

🔴 **HH:MM · MĚNA · Společný název** (např. CPI)
- **Hlavní zpráva** · Forecast X | Previous Y
- Další zpráva · Forecast X | Previous Y
- Další zpráva · Forecast X | Previous Y

Tučně je jen zpráva, která má v datech "hlavni": true, ostatní normálním písmem. \
Hlavní zprávu dej jako první odrážku. Pokud je ve skupině jen jedna zpráva, napiš ji také \
jako tučnou odrážku.

Pod seznam zpráv dej krátký popis:
(přesně 2 krátké odrážky, každá max. jeden řádek, cca 12 slov:)
- Co měří: …
- Dopad na sazby: … (jak výsledek ovlivní rozhodnutí centrální banky o úrokových sazbách)

{IMPACT_BLOCK}
{IMPACT_BLOCK_NOTE}

Pak 🟠 Medium události, jen jeden řádek na událost:
🟠 **HH:MM · MĚNA · Název** · Forecast X | Previous Y · vyšší než očekávaný: AUD/CAD {UP} nebo {DOWN}
(u projevu bez Forecastu: · jestřábí tón: AUD/CAD {UP} nebo {DOWN})

Na konec, jen pokud je co říct, jeden řádek ⚠️ (shluk zpráv do 30 minut, projev guvernéra RBA, \
BoC nebo Fedu, AUD a CAD zprávy ve stejný den).

Chybějící Forecast nebo Previous vynech. Buď stručný a věcný, žádná obchodní doporučení \
(nekupuj/neprodávej). Max. 2500 znaků."""

ALERT_PROMPT = TRADER_PROFILE + IMPACT_RULES + f"""

Za pár minut vyjde High Impact zpráva (případně víc zpráv najednou). Napiš v češtině pro Discord:

Přesně 2 krátké odrážky, každá max. jeden řádek (cca 12 slov):
- Co měří: …
- Dopad na sazby: … (jak výsledek ovlivní rozhodnutí centrální banky o úrokových sazbách)

{IMPACT_BLOCK}
{IMPACT_BLOCK_NOTE}

Jedna věta o možné volatilitě a rozšířeném spreadu.
Hodnoty označuj anglicky Forecast a Previous. Žádné obchodní doporučení, žádné nadpisy, \
max. 600 znaků."""


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


# Nejdůležitější zpráva ve skupině (stejný čas a měna) se píše tučně. Pořadí = priorita.
MAIN_EVENTS = [
    "Federal Funds Rate", "Overnight Rate", "Cash Rate",
    "Non-Farm Employment Change", "Core CPI m/m", "Trimmed Mean CPI q/q",
    "CPI m/m", "CPI q/q", "CPI y/y", "Employment Change",
    "Advance GDP q/q", "GDP q/q", "GDP m/m", "Unemployment Rate",
    "Core Retail Sales m/m", "Retail Sales m/m",
]


def main_event_keys(events):
    groups = {}
    for ev in events:
        groups.setdefault((ev["local"], ev["country"]), []).append(ev)
    keys = set()
    for group in groups.values():
        def priority(ev):
            rank = MAIN_EVENTS.index(ev["title"]) if ev["title"] in MAIN_EVENTS else len(MAIN_EVENTS)
            return (rank, -IMPACT_RANK.get(ev["impact"], 0))
        keys.add(event_key(min(group, key=priority)))
    return keys


def plain_list(events):
    main = main_event_keys(events)
    # Hlavní zpráva skupiny jako první
    events = sorted(events, key=lambda e: (e["local"], e["country"], event_key(e) not in main))
    lines = []
    for ev in events:
        extra = []
        if ev.get("forecast"):
            extra.append(f"Forecast {ev['forecast']}")
        if ev.get("previous"):
            extra.append(f"Previous {ev['previous']}")
        suffix = f" · {' | '.join(extra)}" if extra else ""
        title = f"**{ev['title']}**" if event_key(ev) in main else ev["title"]
        lines.append(
            f"{IMPACT_ICON.get(ev['impact'], '')} {ev['local']:%H:%M} "
            f"`{ev['country']}` {title}{suffix}"
        )
    return "\n".join(lines)


def events_payload(events):
    main = main_event_keys(events)
    return json.dumps([
        {
            "cas": f"{e['local']:%a %d.%m. %H:%M}",
            "mena": e["country"],
            "udalost": e["title"],
            "dopad": e["impact"],
            "forecast": e.get("forecast") or None,
            "previous": e.get("previous") or None,
            "hlavni": event_key(e) in main,
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
            embed["title"] = (TITLE_PREFIX + title)[:256]
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
        return at(today, DAY_START), at(today, NIGHT_START), "☀️ Denní seance", COLOR_MORNING
    return at(today, NIGHT_START), at(today + timedelta(days=1), DAY_START), "🌙 Noční seance", COLOR_EVENING


def resolve_auto_mode(now):
    """GitHub Actions spouští cron v UTC. Kvůli letnímu/zimnímu času máme 2 crony
    pro každé upozornění a podle *plánovaného* času (ne skutečného, Actions mívá
    zpoždění) vybereme ten, který odpovídá 6:00 / 20:00 místního času."""
    schedule = os.getenv("SCHEDULE", "")  # např. "0 6 * * *"
    if not schedule:
        hour = now.astimezone(TZ).hour
    else:
        minute, utc_hour = int(schedule.split()[0]), int(schedule.split()[1])
        planned = now.replace(hour=utc_hour, minute=minute, second=0, microsecond=0)
        hour = planned.astimezone(TZ).hour
    return {MORNING_SEND_HOUR: "morning", EVENING_SEND_HOUR: "evening"}.get(hour)


def run_session(mode, now, args):
    start, end, label, color = session_window(mode, now)
    events = filter_events(fetch_events(), start, end, MIN_IMPACT)
    # Nadpis embedu na Discordu neumí zalomit řádek, proto je datum jako první tučný řádek textu
    title = label
    period = f"**{start:%d.%m. %H:%M} – {end:%d.%m. %H:%M}**"
    print(f"{title} {period}: {len(events)} událostí")

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

    text = f"{period}\n\n{text}"
    if args.dry_run:
        print(f"[barva {color:#08x}] {title}\n{text}")
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

    # Test se simulovaným časem nic neukládá, aby nezablokoval skutečné upozornění
    if not args.dry_run and not args.now:
        save_state(state, now)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["morning", "evening", "auto", "alert"], default="auto")
    p.add_argument("--dry-run", action="store_true", help="nic neposílat, jen vypsat")
    p.add_argument("--no-ai", action="store_true", help="bez Claude, jen seznam událostí")
    p.add_argument("--now", default=os.getenv("BOT_NOW") or None,
                   help="simulovaný čas pro test, např. 2026-10-19T06:00")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    if args.now:
        now = datetime.fromisoformat(args.now.strip()).replace(tzinfo=TZ).astimezone(timezone.utc)
        global TITLE_PREFIX
        TITLE_PREFIX = "🧪 TEST • "
        print(f"Simulovaný čas: {now.astimezone(TZ):%d.%m.%Y %H:%M}")

    if args.mode == "alert":
        run_alert(now, args)
        return

    mode = args.mode if args.mode != "auto" else resolve_auto_mode(now)
    if mode is None:
        print("Teď není čas odeslání přehledu (6:00 / 20:00 místního času) – končím.")
        return
    # Víkendová pauza platí jen pro automatické (plánované) spuštění, ruční test projde vždy
    if args.mode == "auto":
        weekday = now.astimezone(TZ).weekday()
        if weekday not in (MORNING_DAYS if mode == "morning" else EVENING_DAYS):
            print("Víkend – přehled se dnes neposílá.")
            return
    run_session(mode, now, args)


if __name__ == "__main__":
    main()
