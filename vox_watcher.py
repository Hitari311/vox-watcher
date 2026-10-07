#!/usr/bin/env python3
"""
VOX Cinemas Egypt - new movie watcher
-------------------------------------
Watches the "What's On", "Coming Soon" and per-cinema showtimes pages and
alerts you when:
  * NEW MOVIE      - a movie slug appears that was never seen before
  * BOOKING OPEN   - a movie shows up on a cinema's showtimes page for the first time
It also logs every change with a timestamp (Cairo time) so that, after a week or
two, `--report` tells you on which days/hours the site usually gets updated.

Setup:
    pip install requests beautifulsoup4
    pip install curl_cffi            # optional, used automatically if you get 403s

Telegram alerts are configured below in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
(Environment variables of the same names override them if you ever set any.)

Usage:
    python vox_watcher.py              # run forever (checks every ~15 min)
    python vox_watcher.py --once       # single check (good for cron / Task Scheduler)
    python vox_watcher.py --report     # show when updates usually happen
    python vox_watcher.py --test       # check that every page can be fetched
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    sys.exit(f"Missing package ({e.name}). Run:\n"
             f"    python -m pip install requests beautifulsoup4 tzdata")

# ----------------------------- config ---------------------------------------
BASE = "https://egy.voxcinemas.com"
CINEMAS = ["city-centre-alexandria", "city-centre-almaza", "mall-of-egypt"]
CINEMA_NAMES = {                     # how cinemas are named in alerts
    "city-centre-alexandria": "City Centre Alexandria",
    "city-centre-almaza": "City Centre Almaza",
    "mall-of-egypt": "Mall of Egypt",
}

SOURCES = {
    "whatson": f"{BASE}/movies/whatson",
    "comingsoon": f"{BASE}/movies/comingsoon",
    **{f"showtimes:{c}": f"{BASE}/showtimes/{c}" for c in CINEMAS},
}

# --- Telegram alerts (env vars override these if set) ---
TELEGRAM_BOT_TOKEN = ""      # left empty on purpose: set as GitHub Actions secrets
TELEGRAM_CHAT_ID = ""        # (env vars of the same names are used instead)

MAX_DAYS = 14                # look this many days ahead for published showtimes
EMPTY_STOP = 2               # stop scanning a cinema after this many empty days in a row

INTERVAL_MIN = 15            # minutes between checks (keep it polite)
JITTER_SEC = 90              # random extra wait so requests aren't perfectly periodic
try:
    TZ = ZoneInfo("Africa/Cairo")
except ZoneInfoNotFoundError:
    sys.exit("Timezone data not found (common on Windows). Run:\n"
             "    python -m pip install tzdata\n"
             "then start the script again.")

DATA_DIR = Path(__file__).with_name("vox_data")
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "changes.csv"

# /movies/<slug> paths that are listing pages, not movies
NON_MOVIE_SLUGS = {"whatson", "comingsoon", "now-showing", "coming-soon", "search", "all"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

MOVIE_HREF = re.compile(r"^(?:https?://egy\.voxcinemas\.com)?/movies/([a-z0-9][a-z0-9\-]*)/?(?:[?#].*)?$", re.I)


# ----------------------------- helpers --------------------------------------
def now():
    return datetime.now(TZ)


def fetch(url):
    """Return (html, headers).

    Tries curl_cffi first (it mimics Chrome's TLS fingerprint, which many
    bot-protection systems check), then plain requests. Retries once on
    connection resets.
    """
    errors = []
    for attempt in range(2):
        try:
            from curl_cffi import requests as creq
            r = creq.get(url, impersonate="chrome", timeout=30,
                         headers={"Accept-Language": HEADERS["Accept-Language"]})
            if r.status_code == 200:
                return r.text, dict(r.headers)
            errors.append(f"curl_cffi HTTP {r.status_code}")
        except ImportError:
            if attempt == 0:
                errors.append("curl_cffi not installed")
        except Exception as e:
            errors.append(f"curl_cffi {type(e).__name__}")

        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text, dict(r.headers)
            errors.append(f"requests HTTP {r.status_code}")
        except requests.RequestException as e:
            errors.append(f"requests {type(e).__name__}")

        time.sleep(5)
    raise RuntimeError("; ".join(errors))


JUNK_TEXT = {"info", "more info", "view movie", "showtimes", "book now", "book",
             "buy tickets", "watch trailer", "trailer", "details"}
POSTER_PREFIX = re.compile(r"^(?:movie\s+)?poster\s+(?:for|of)\s+", re.I)
# age ratings VOX puts before titles: 18TC, 12+, 16+, PG, PG-13, E, TBC ...
RATING_PREFIX = re.compile(r"^(?:\d{1,2}\s*(?:\+|TC)|PG-?\d{0,2}|TBC|G|E)\s+", re.I)


def clean_title(raw):
    t = " ".join((raw or "").split())
    t = POSTER_PREFIX.sub("", t)
    for _ in range(2):                       # e.g. "18TC 18+ Title"
        t = RATING_PREFIX.sub("", t)
    t = t.strip(" -|:")
    if not t or t.lower() in JUNK_TEXT or len(t) > 80:   # >80 = whole card text, not a title
        return ""
    return t


def extract_movies(html):
    """Return {slug: title} for every movie link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    best = {}                                 # slug -> (priority, title)
    for a in soup.find_all("a", href=True):
        m = MOVIE_HREF.match(a["href"].strip())
        if not m:
            continue
        slug = m.group(1).lower()
        if slug in NON_MOVIE_SLUGS:
            continue
        img = a.find("img")
        candidates = [                        # lower number = more trustworthy
            (0, clean_title(img.get("alt")) if img else ""),
            (1, clean_title(a.get("title"))),
            (2, clean_title(a.get_text(" ", strip=True))),
        ]
        best.setdefault(slug, (9, ""))
        for prio, title in candidates:
            if title and prio < best[slug][0]:
                best[slug] = (prio, title)
    return {s: (t or slug_to_title(s)) for s, (_, t) in best.items()}


LANG_SUFFIXES = ("arabic", "hindi", "english", "french", "turkish", "korean", "japanese")


def slug_to_title(slug):
    """'red-flag-arabic' -> 'Red Flag (Arabic)' (used only when a page has no real title)."""
    words = slug.split("-")
    lang = words.pop() if len(words) > 1 and words[-1] in LANG_SUFFIXES else None
    title = " ".join(words).title()
    return f"{title} ({lang.title()})" if lang else title


TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[ap]\.?m\b", re.I)


def extract_scheduled(html):
    """Movies on a showtimes page that actually have session times listed.

    VOX shows a generic movie listing for dates with no sessions, so counting
    every movie link there gives false results. Walking the page in order, each
    time like "3:45pm" belongs to the most recent movie link above it.
    """
    soup = BeautifulSoup(html, "html.parser")
    current, scheduled = None, set()
    for node in soup.descendants:
        name = getattr(node, "name", None)
        if name == "a" and node.get("href"):
            m = MOVIE_HREF.match(node["href"].strip())
            if m and m.group(1).lower() not in NON_MOVIE_SLUGS:
                current = m.group(1).lower()
        elif name is None and current and TIME_RE.search(str(node)):
            if node.parent is not None and node.parent.name not in ("script", "style"):
                scheduled.add(current)
    titles = extract_movies(html)
    return {slug: titles.get(slug, slug_to_title(slug)) for slug in scheduled}


def is_fallback_title(slug, title):
    return title == slug_to_title(slug)


def collect(name, url, verbose=False):
    """Return ({slug: title}, headers, schedule) for one source.

    Showtimes pages list ONE day, so for each cinema we walk forward day by day
    until EMPTY_STOP empty days in a row (the end of what VOX has published),
    capped at MAX_DAYS. schedule = {"dates": {"YYYY-MM-DD": [slugs]}, "edge": ...}
    and is None for non-showtimes pages. A failed request raises, which skips
    the whole source for this run.
    """
    if not name.startswith("showtimes:"):
        html, headers = fetch(url)
        return extract_movies(html), headers, None

    cinema = name.split(":", 1)[1]
    merged, headers, dates = {}, {}, {}
    empty_run, edge = 0, None
    for i in range(MAX_DAYS):
        day = now() + timedelta(days=i)
        html, headers = fetch(f"{BASE}/showtimes?c={cinema}&d={day:%Y%m%d}")
        found = extract_scheduled(html)
        if verbose:
            print(f"        {day:%a %d %b}: {len(extract_movies(html)):>3} listed, "
                  f"{len(found):>3} with showtimes")
        if found:
            dates[f"{day:%Y-%m-%d}"] = sorted(found)
            empty_run = 0
            if i == MAX_DAYS - 1:
                edge = f"{day:%Y-%m-%d}"   # window edge: may just be the window rolling
        elif i > 0:
            empty_run += 1
            if empty_run >= EMPTY_STOP:
                break
        for slug, title in found.items():
            if slug not in merged or (is_fallback_title(slug, merged[slug])
                                      and not is_fallback_title(slug, title)):
                merged[slug] = title
        time.sleep(random.uniform(1, 3))
    return merged, headers, {"dates": dates, "edge": edge}


def fmt_day(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"movies": {}, "sources": {}}


def save_state(state):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def log_change(source, added, removed, headers):
    DATA_DIR.mkdir(exist_ok=True)
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time_cairo", "weekday", "source", "added", "removed",
                        "last_modified", "etag"])
        t = now()
        w.writerow([t.isoformat(timespec="seconds"), t.strftime("%A"), source,
                    ";".join(added), ";".join(removed),
                    headers.get("Last-Modified", ""), headers.get("ETag", "")])


def notify(msg):
    print(f"[{now():%Y-%m-%d %H:%M}] {msg}")
    token = os.getenv("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    chat = os.getenv("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    if not (token and chat):
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": msg,
                                "disable_web_page_preview": "true"}, timeout=15)
        if r.status_code != 200:
            print(f"  (telegram rejected it: HTTP {r.status_code} {r.text[:200]})")
    except requests.RequestException as e:
        print(f"  (telegram failed: {e})")


# ----------------------------- core -----------------------------------------
def best_title(state, slug, movies):
    """Prefer a real title from any page over a slug-based fallback."""
    t = state["movies"].get(slug, {}).get("title") or movies.get(slug, slug)
    return t


def check_once(state):
    first_run = not state["sources"]
    ts = now().isoformat(timespec="seconds")
    ok = changed = 0
    pending = {}     # slug -> {"first": bool, "cinemas": [...]} showtimes alerts for this run
    date_events = [] # (cinema, [new dates], schedule) - new showtime dates released
    state.setdefault("schedule", {})

    for name, url in SOURCES.items():
        try:
            movies, headers, sched = collect(name, url)
        except Exception as e:
            print(f"[{now():%H:%M}] {name}: fetch failed ({e})")
            continue
        ok += 1

        if sched is not None and sched["dates"]:
            cinema = name.split(":", 1)[1]
            prev_last = state["schedule"].get(cinema, {}).get("last_date")
            if prev_last and not first_run:
                new_dates = sorted(d for d in sched["dates"] if d > prev_last)
                if new_dates == [sched["edge"]]:   # only the 14-day window rolled forward
                    new_dates = []
                if new_dates:
                    date_events.append((cinema, new_dates, sched))
                    log_change(f"dates:{cinema}", new_dates, [], headers)
            state["schedule"][cinema] = {
                "last_date": max([*sched["dates"], *([prev_last] if prev_last else [])]),
                "checked": ts,
            }

        if not movies:
            print(f"[{now():%H:%M}] {name}: 0 movies parsed - page may be JS-rendered or blocked")
            continue

        digest = hashlib.sha256("\n".join(sorted(movies)).encode()).hexdigest()
        prev = state["sources"].get(name, {"hash": None, "slugs": []})
        if digest == prev["hash"]:
            continue
        changed += 1

        old = set(prev["slugs"])
        added = sorted(set(movies) - old)
        removed = sorted(old - set(movies))
        if prev["hash"] is not None:
            log_change(name, added, removed, headers)

        for slug in added:
            title = movies[slug]
            link = f"{BASE}/movies/{slug}"
            known = slug in state["movies"]
            rec = state["movies"].setdefault(slug, {"title": title, "first_seen": ts, "seen_in": {}})
            if is_fallback_title(slug, rec["title"]) and not is_fallback_title(slug, title):
                rec["title"] = title
            had_showtimes = any(x.startswith("showtimes:") for x in rec["seen_in"])
            new_here = name not in rec["seen_in"]
            rec["seen_in"].setdefault(name, ts)
            title = best_title(state, slug, movies)

            if first_run:
                continue
            if name.startswith("showtimes:"):
                if new_here:       # first time this movie appears at this cinema
                    cinema = name.split(":", 1)[1]
                    entry = pending.setdefault(slug, {"first": not had_showtimes, "cinemas": []})
                    entry["cinemas"].append(CINEMA_NAMES.get(cinema, cinema))
            elif not known:
                notify(f"🎬 NEW MOVIE on VOX Egypt ({name}): {title}\n{link}")
            elif name == "whatson" and "comingsoon" in rec["seen_in"]:
                notify(f"▶️ Now showing: {title}\n{link}")

        state["sources"][name] = {"hash": digest, "slugs": sorted(movies), "changed": ts}
        time.sleep(random.uniform(3, 8))   # be gentle between pages

    for slug, entry in pending.items():
        title = best_title(state, slug, {})
        where = ", ".join(entry["cinemas"])
        link = f"{BASE}/movies/{slug}"
        if entry["first"]:
            notify(f"🎟️ BOOKING OPEN: {title} at {where}\n{link}")
        else:
            notify(f"📍 {title} now also showing at {where}\n{link}")

    for cinema, new_dates, sched in date_events:
        slugs = sorted({x for d in new_dates for x in sched["dates"][d]})
        titles = [best_title(state, x, {}) for x in slugs]
        shown = ", ".join(titles[:8]) + (f" (+{len(titles) - 8} more)" if len(titles) > 8 else "")
        span = (fmt_day(new_dates[0]) if len(new_dates) == 1
                else f"{fmt_day(new_dates[0])} → {fmt_day(new_dates[-1])} ({len(new_dates)} days)")
        notify(f"🗓️ New showtimes released at {CINEMA_NAMES.get(cinema, cinema)}\n"
               f"{span}\n{shown}\n{BASE}/showtimes/{cinema}")

    if first_run:
        print(f"Baseline saved: {len(state['movies'])} movies. "
              "You'll be alerted about anything new from now on.")
    print(f"[{now():%Y-%m-%d %H:%M}] {ok}/{len(SOURCES)} pages OK, "
          f"{changed} changed, {len(state['movies'])} movies known")
    save_state(state)


def report():
    if not LOG_FILE.exists():
        print("No changes logged yet - let the watcher run for a week or two.")
        return
    rows = list(csv.DictReader(LOG_FILE.open(encoding="utf-8")))
    rows = [r for r in rows if r["added"]]          # only count real additions
    if not rows:
        print("Changes logged, but no additions yet.")
        return
    days = Counter(r["weekday"] for r in rows)
    hours = Counter(datetime.fromisoformat(r["time_cairo"]).hour for r in rows)
    print(f"{len(rows)} update events with new movies (Cairo time)\n")
    print("By weekday:")
    for d, n in days.most_common():
        print(f"  {d:<10} {'#' * n} {n}")
    print("\nBy hour (the change happened within INTERVAL_MIN before this):")
    for h in sorted(hours):
        print(f"  {h:02d}:00  {'#' * hours[h]} {hours[h]}")
    print("\nMost recent:")
    for r in rows[-10:]:
        print(f"  {r['time_cairo']}  {r['source']:<32} +{r['added']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single check and exit")
    ap.add_argument("--report", action="store_true", help="show when the site usually updates")
    ap.add_argument("--test", action="store_true", help="test each page once, change nothing")
    args = ap.parse_args()

    if args.test:
        for name, url in SOURCES.items():
            try:
                found, _, _ = collect(name, url, verbose=True)
                print(f"OK    {name:<32} {len(found)} movies"
                      + (f"  e.g. {list(found.values())[:3]}" if found else ""))
            except Exception as e:
                print(f"FAIL  {name:<32} {e}")
        return

    if args.report:
        return report()

    state = load_state()
    if args.once:
        return check_once(state)

    print(f"Watching {len(SOURCES)} pages every ~{INTERVAL_MIN} min. Ctrl+C to stop.")
    while True:
        try:
            check_once(state)
        except Exception as e:                       # never die on a bad cycle
            print(f"cycle error: {e}", file=sys.stderr)
        time.sleep(INTERVAL_MIN * 60 + random.uniform(0, JITTER_SEC))


if __name__ == "__main__":
    main()
