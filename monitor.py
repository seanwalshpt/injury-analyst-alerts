#!/usr/bin/env python3
"""
The Injury Analyst: breaking sports injury monitor.

Every run:
  1. Pulls recent headlines from Google News RSS searches and ESPN RSS feeds.
  2. Keeps only fresh, injury-related headlines it hasn't processed before.
  3. Groups headlines about the same injury into one "story" (so five outlets
     reporting the same injury = one story with five sources, not five alerts).
  4. Scores each story 0-100 and sends a Telegram alert when it crosses the
     alert threshold, plus an UPDATE alert if it later becomes much bigger
     (e.g. jumps to high priority or a torn ACL gets confirmed).

Usage:
  python monitor.py              normal run (needs Telegram secrets)
  python monitor.py --test       send a test Telegram message and exit
  python monitor.py --dry-run    run without Telegram; print alerts instead
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
STATE_FILE = ROOT / "state" / "state.json"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (injury-analyst-alerts; +https://github.com)"}
NOW = datetime.now(timezone.utc)
EASTERN = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Injury vocabulary. Each phrase is matched as whole words, case-insensitive.
# ---------------------------------------------------------------------------

SEVERE = [  # 25 pts: likely long-term or visually dramatic
    "torn acl", "acl tear", "tore his acl", "torn mcl", "mcl tear", "torn achilles",
    "achilles tear", "ruptured achilles", "achilles", "torn meniscus", "meniscus tear",
    "torn labrum", "labrum tear", "torn pec", "pec tear", "pectoral tear", "torn hamstring",
    "hamstring tear", "partial tear", "torn", "ruptured", "season-ending", "season ending",
    "out for the season", "out for season", "out for the year", "carted off", "carted",
    "stretcher", "stretchered", "broken leg", "broken ankle", "broken arm", "broken hand",
    "broken foot", "broken wrist", "broken finger", "broken collarbone", "broken rib",
    "broken ribs", "broken bone", "fractured", "fracture", "dislocated", "dislocation", "surgery",
    "career-threatening", "neck injury", "spinal",
    # Medical (non-orthopedic) conditions
    "blood clot", "blood clots", "clot", "clots", "dvt", "deep vein thrombosis",
    "pulmonary embolism", "embolism", "cardiac", "cardiac arrest", "heart condition",
    "heart issue", "heart attack", "myocarditis", "arrhythmia", "collapsed on", "seizure",
    "suffered a stroke", "had a stroke", "brain bleed", "life-threatening", "icu", "intensive care",
    "out indefinitely",
]
HIGH = [  # 18 pts: left the game / real concern
    "won't return", "will not return", "not return", "ruled out for the rest",
    "ruled out for remainder", "out for the rest of the game", "out for the game",
    "left the game", "leaves game", "leaves the game", "left game", "exits game",
    "exits", "exited", "locker room", "helped off", "limped off", "limps off",
    "concussion", "concussion protocol", "head injury", "mri", "x-rays", "x-ray",
    "ambulance", "hospital", "hospitalized", "went down", "goes down", "down on the field",
    "down on the court", "injury scare", "in pain", "will miss", "to miss", "sidelined",
    "ruled out", "injured reserve", "10-day il", "15-day il", "60-day il", "injured list",
    # Held out before a game
    "held out", "won't play", "will not play",
    # Practice injuries
    "injured in practice", "injured at practice", "hurt in practice", "hurt at practice",
    "practice injury", "left practice", "leaves practice", "exits practice",
    # Medical / illness storylines
    "medical condition", "medical issue", "health scare", "health issue", "non-football injury",
    "appendicitis", "appendectomy", "pneumonia", "infection", "kidney", "diagnosed",
    "diagnosis", "setback", "aggravated", "re-injured", "reinjured", "further tests",
    "more tests", "underwent tests", "undergo tests",
]
# Milestone news on an existing storyline (worth an UPDATE even though it's
# good news): someone you've been following is getting back on the field.
MILESTONES = [
    "cleared", "activated", "reinstated", "returns to practice", "back at practice",
    "set to return", "expected to return", "will return", "will play", "will start",
    "designated to return", "21-day window", "back in lineup", "back in the lineup",
    # Diagnosis / imaging results after an exit (good or bad, it's new info)
    "bone bruise", "sprain", "sprained", "high ankle sprain", "strain", "strained",
    "grade 1", "grade 2", "grade 3", "mri results", "mri revealed", "mri reveals",
    "mri showed", "mri shows", "mri confirms", "mri confirmed", "imaging", "contusion",
    "hyperextended", "hyperextension", "subluxation", "no structural damage",
    "avoids serious injury", "avoided serious injury", "dodges serious injury",
    "re-evaluated", "week-to-week", "day-to-day", "expected to miss", "timeline",
]
MEDIUM = [  # 12 pts: injury mentioned, severity unclear
    "questionable to return", "doubtful to return", "being evaluated", "evaluated",
    "medical tent", "blue tent", "injured", "injury", "injuries", "injures", "hurt",
    "hamstring", "ankle", "knee", "shoulder", "groin", "calf", "foot", "hip", "wrist",
    "elbow", "oblique", "quad", "back spasms", "sprain", "sprained", "strain", "strained",
    "bruise", "contusion", "illness", "sick", "flu", "virus", "medical", "hospital visit",
]
LOW = [  # 5 pts: routine status news
    "injury report", "day-to-day", "limited practice", "did not practice", "dnp",
    "questionable", "doubtful", "probable", "load management", "inactive", "sit out",
    "sits out", "to sit", "will sit", "rest day",
]
POSITIVE = [  # good news: caps severity so "cleared to return" doesn't alert
    "cleared", "activated", "reinstated", "returns to practice", "back from injury",
    "return from injury", "returns from injury", "returning from injury", "set to return",
    "expected to return", "will return", "expected to play", "will play", "good to go",
    "no structural damage", "avoids serious", "avoided serious", "dodges", "x-rays negative",
    "negative x-rays", "not serious", "minor injury", "back in lineup", "back in the lineup",
]
# Bad-news phrases that contain good-news words; removed before checking POSITIVE.
NEGATED = re.compile(
    r"\b(?:not|isn't|is not|unlikely|questionable|doubtful|won't|will not)\s+"
    r"(?:expected\s+)?(?:to\s+)?(?:return|play)\b"
)
MECHANISM = {  # content potential: is there a clip worth breaking down?
    "non-contact": 5, "noncontact": 5, "awkward": 4, "awkwardly": 4, "gruesome": 4,
    "scary": 3, "video": 3, "watch": 3, "replay": 3, "grabbed": 3, "grabbing": 3,
    "clutching": 3, "landed": 3, "collision": 3, "planted": 3, "hyperextended": 4,
    "rolled": 3, "twisted": 3, "tackle": 2, "hit": 1,
}
NOISE = [  # roundups and betting content: not breaking injury news
    "fantasy", "start/sit", "start sit", "start 'em", "waiver", "waivers", "sleepers",
    "odds", "betting", "bets", "props", "parlay", "dfs", "injury report", "injury updates",
    "injury roundup", "injury tracker", "injury news:", "every injury", "all the injuries",
    "picks", "prediction", "predictions", "power rankings", "mock draft",
]
STOPWORDS = set(
    "a an the and or of to in on at for with from by as is are was were be been his her "
    "their its after before during vs v. vs. says say said will not out game games "
    "injury injured injuries update updates news latest report reports status week "
    "nfl nba mlb nhl mls night sunday monday tuesday wednesday thursday friday saturday "
    "today vs new more what how why who".split()
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def deaccent(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).replace("\u2019", "'")


def normalize(text: str) -> str:
    """Lowercase, strip accents, unify quotes and whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'").replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def phrase_pattern(phrase: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(normalize(phrase)) + r"(?![a-z0-9])")


def compile_terms(terms):
    return [(t, phrase_pattern(t)) for t in terms]


SEVERE_RX, HIGH_RX, MEDIUM_RX, LOW_RX = map(compile_terms, (SEVERE, HIGH, MEDIUM, LOW))
POSITIVE_RX, NOISE_RX = compile_terms(POSITIVE), compile_terms(NOISE)
MILESTONE_RX = compile_terms(MILESTONES)
MECHANISM_RX = [(t, phrase_pattern(t), pts) for t, pts in MECHANISM.items()]
QB_RX = phrase_pattern("quarterback"), phrase_pattern("qb")
STAR_RX = phrase_pattern("star")


def matches(rx_list, text):
    return [t for t, rx in rx_list if rx.search(text)]


def drop_subphrases(terms):
    """['carted', 'carted off'] -> ['carted off']"""
    terms = sorted(set(terms))
    return [t for t in terms if not any(t != o and t in o for o in terms)]


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", normalize(title))
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


SETTINGS = load_json(CONFIG_DIR / "settings.json")
TIER_POINTS = {"tier1": 30, "tier2": 25, "tier3": 20, "roster": 10}


def build_player_index():
    """List of dicts: league, tier, name, patterns."""
    data = load_json(CONFIG_DIR / "players.json")
    index = []
    for league, tiers in data.items():
        if league.startswith("_"):
            continue
        for tier, entries in tiers.items():
            for entry in entries:
                names = [n.strip() for n in entry.split("|") if n.strip()]
                # "=Hurts" means: match only with exact capitalization, and only
                # in that league's news (for last names that are also common words).
                exact = [n[1:] for n in names[1:] if n.startswith("=")]
                plain = [names[0]] + [n for n in names[1:] if not n.startswith("=")]
                index.append({
                    "league": league,
                    "tier": tier,
                    "name": names[0],
                    "patterns": [phrase_pattern(n) for n in plain],
                    "exact_patterns": [
                        re.compile(r"(?<![A-Za-z0-9])" + re.escape(deaccent(n)) + r"(?![A-Za-z0-9])")
                        for n in exact
                    ],
                })
    return index


def build_team_index():
    data = load_json(CONFIG_DIR / "teams.json")
    return {
        league: [(team, phrase_pattern(team)) for team in teams]
        for league, teams in data.items() if not league.startswith("_")
    }


PLAYERS = build_player_index()
TEAMS = build_team_index()


def find_player(text: str, feed_league: str, raw: str = ""):
    """Best player match: prefer the feed's league, then higher tier."""
    found = find_players(text, feed_league, raw)
    return found[0] if found else None


def find_players(text: str, feed_league: str, raw: str = ""):
    """Every recognized player in the text, best match first."""
    raw = deaccent(raw)
    hits = [
        p for p in PLAYERS
        if any(rx.search(text) for rx in p["patterns"])
        or (p["league"] == feed_league and any(rx.search(raw) for rx in p["exact_patterns"]))
    ]
    hits.sort(key=lambda p: (p["league"] != feed_league, p["tier"]))
    names = {p["name"] for p in hits}
    hits += [p for p in find_rostered(text, feed_league) if p["name"] not in names]
    return hits


# ---------------------------------------------------------------------------
# Rosters: recognize every current player, not just the hand-picked list.
# Pulled from ESPN's public team rosters, refreshed daily, one league per run.
# ---------------------------------------------------------------------------

ROSTER_FILE = ROOT / "state" / "rosters.json"
ROSTER_SPORTS = {"nfl": "football/nfl", "nba": "basketball/nba", "mlb": "baseball/mlb", "nhl": "hockey/nhl"}
NAME_TOKEN = re.compile(r"[a-z0-9][a-z0-9.'-]*")
ROSTERS = {"updated": {}, "players": {}}


def name_tokens(text: str) -> list[str]:
    out = []
    for t in NAME_TOKEN.findall(normalize(text)):
        t = re.sub(r"(?:'s|')$", "", t).rstrip(".-")  # Suggs' / Suggs's -> suggs
        if t:
            out.append(t)
    return out


def _walk(obj, key):
    """Yield every dict inside obj that has `key`."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from _walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, key)


def load_rosters():
    global ROSTERS
    if ROSTER_FILE.exists():
        try:
            ROSTERS = load_json(ROSTER_FILE)
        except (json.JSONDecodeError, OSError):
            pass


def refresh_rosters(budget_seconds: int = 30):
    """Refresh the stalest league's rosters if it's over a day old."""
    epoch = "2000-01-01T00:00:00+00:00"
    attempted = ROSTERS.setdefault("attempted", {})
    stale = [
        lg for lg in ROSTER_SPORTS
        if NOW - parse_iso(ROSTERS["updated"].get(lg, epoch)) > timedelta(hours=24)
        and NOW - parse_iso(attempted.get(lg, epoch)) > timedelta(minutes=30)
    ]
    if not stale:
        return
    # Least recently attempted first, so one failing league can't block the rest.
    league = min(stale, key=lambda lg: attempted.get(lg, epoch))
    attempted[league] = iso(NOW)
    save_rosters()
    base = f"https://site.api.espn.com/apis/site/v2/sports/{ROSTER_SPORTS[league]}"
    started = time.monotonic()
    try:
        r = requests.get(f"{base}/teams", headers=HTTP_HEADERS, timeout=10)
        r.raise_for_status()
        teams = {t["id"]: t.get("name") or t.get("displayName") for t in _walk(r.json(), "id")
                 if "abbreviation" in t and "location" in t}
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  ! roster refresh failed ({league}): {type(e).__name__}")
        return
    players = {}
    for team_id, team_name in teams.items():
        if time.monotonic() - started > budget_seconds:
            print(f"  ! roster refresh for {league.upper()} ran out of time; will retry next run")
            return
        try:
            r = requests.get(f"{base}/teams/{team_id}/roster", headers=HTTP_HEADERS, timeout=8)
            r.raise_for_status()
        except requests.RequestException:
            continue
        for athlete in _walk(r.json(), "fullName"):
            key = " ".join(name_tokens(athlete["fullName"]))
            if len(key.split()) >= 2:
                players[key] = {"name": athlete["fullName"], "team": team_name}
    if len(players) < 100:  # something went wrong; keep the old list
        print(f"  ! roster refresh for {league.upper()} looked incomplete ({len(players)} players)")
        return
    ROSTERS["players"][league] = players
    ROSTERS["updated"][league] = iso(NOW)
    save_rosters()
    print(f"Rosters refreshed: {league.upper()} ({len(players)} players)")


def save_rosters():
    ROSTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ROSTER_FILE.write_text(json.dumps(ROSTERS), encoding="utf-8")


def find_rostered(text: str, feed_league: str):
    toks = name_tokens(text)
    grams = {" ".join(toks[i:i + n]) for n in (2, 3, 4) for i in range(len(toks) - n + 1)}
    order = [feed_league] + [lg for lg in ROSTERS["players"] if lg != feed_league]
    found = []
    for league in order:
        roster = ROSTERS["players"].get(league, {})
        for g in sorted(grams, key=lambda g: toks_index(toks, g)):
            if g in roster:
                p = roster[g]
                found.append({"league": league, "tier": "roster", "name": p["name"], "team": p["team"]})
    return found


def roster_team(name: str, league: str):
    """Current team from ESPN rosters (more reliable than guessing from a
    headline that may mention two teams)."""
    p = ROSTERS["players"].get(league, {}).get(" ".join(name_tokens(name)))
    return p["team"] if p else None


def toks_index(toks, gram):
    """Position of a name in the headline, so the first-named player wins."""
    first = gram.split()[0]
    return toks.index(first) if first in toks else len(toks)


def find_team(text: str, league: str):
    for team, rx in TEAMS.get(league, []):
        if rx.search(text):
            return team
    return None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def google_news_url(query: str) -> str:
    return (
        "https://news.google.com/rss/search?q="
        + quote_plus(f"{query} when:2h")
        + "&hl=en-US&gl=US&ceid=US:en"
    )


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(url: str, league: str, is_google: bool):
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=8)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  ! feed failed ({league}): {url[:90]} -> {e}")
        return []
    parsed = feedparser.parse(resp.content)
    items = []
    for e in parsed.entries:
        title = html.unescape(e.get("title", "")).strip()
        link = e.get("link", "").strip()
        if not title or not link:
            continue
        source = ""
        if is_google:
            source = (e.get("source") or {}).get("title", "")
            # Google titles look like "Headline - Publisher"; drop the publisher.
            if source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)].strip()
        else:
            source = (parsed.feed.get("title") or "ESPN").split(" - ")[0].strip() or "ESPN"
        summary = ""
        if not is_google:  # Google summaries just repeat the title as HTML
            summary = re.sub(r"<[^>]+>", " ", html.unescape(e.get("summary", "")))
        items.append({
            "title": title,
            "link": link,
            "source": source or "Unknown",
            "summary": summary,
            "published": entry_time(e) or NOW,
            "league": league,
        })
    return items


def fetch_all():
    jobs = []
    for league, queries in SETTINGS["google_news_queries"].items():
        jobs += [(google_news_url(q), league, True) for q in queries]
    for league, urls in SETTINGS.get("extra_rss_feeds", {}).items():
        jobs += [(u, league, False) for u in urls]
    return run_jobs(jobs)


def fetch_live(leagues):
    """Smaller, faster set of in-game searches, only for leagues playing now."""
    lm = SETTINGS["live_mode"]
    jobs = []
    for league in leagues:
        jobs += [(google_news_url(q), league, True) for q in lm["live_queries"].get(league, [])]
        jobs += [(u, league, False) for u in SETTINGS.get("extra_rss_feeds", {}).get(league, [])]
    return run_jobs(jobs)


def run_jobs(jobs, budget_seconds: int = 100):
    """Fetch feeds, but stop early if the news sites are being slow, so a run
    never overlaps the next one or hits the job time limit."""
    items = []
    started = time.monotonic()
    for i, (url, league, is_google) in enumerate(jobs):
        if time.monotonic() - started > budget_seconds:
            print(f"  ! time budget reached; skipped {len(jobs) - i} feeds this run")
            break
        items += fetch_feed(url, league, is_google)
    print(f"Fetched {len(items)} headlines from {len(jobs)} feeds")
    return items


# ---------------------------------------------------------------------------
# Live game detection (ESPN's public scoreboard)
# ---------------------------------------------------------------------------

def detect_live_games() -> dict:
    """{league: {"teams": set(team names), "why": str}} for leagues with a game
    in progress, about to start, or just finished (post-game injury news).
    If ESPN can't be reached, returns {} and the monitor runs at normal speed."""
    lm = SETTINGS.get("live_mode", {})
    if not lm.get("enabled"):
        return {}
    live = {}
    for league, urls in lm["scoreboards"].items():
        game_len = timedelta(hours=lm["game_hours"].get(league, 3))
        pre = timedelta(minutes=lm["pregame_minutes"])
        post = timedelta(minutes=lm["postgame_minutes"])
        for url in urls:
            try:
                resp = requests.get(url, headers=HTTP_HEADERS, timeout=10)
                resp.raise_for_status()
                events = resp.json().get("events", [])
            except (requests.RequestException, ValueError) as e:
                print(f"  ! scoreboard failed ({league}): {type(e).__name__}")
                continue
            for ev in events:
                state = ((ev.get("status") or {}).get("type") or {}).get("state")
                try:
                    start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue
                if state == "in":
                    why = "in progress"
                elif state == "pre" and start - pre <= NOW:
                    why = "starting soon"
                elif state == "post" and NOW <= start + game_len + post:
                    why = "just ended"
                else:
                    continue
                entry = live.setdefault(league, {"teams": set(), "why": why})
                if why == "in progress":
                    entry["why"] = why
                for comp in ev.get("competitions", [])[:1]:
                    for c in comp.get("competitors", []):
                        team = c.get("team") or {}
                        for key in ("name", "shortDisplayName", "displayName"):
                            if team.get(key):
                                entry["teams"].add(team[key])
    return live


# ---------------------------------------------------------------------------
# Analysis and scoring
# ---------------------------------------------------------------------------

def analyze(item: dict):
    """Return analysis dict, or None if this isn't an injury story."""
    title_n = normalize(item["title"])
    text_n = normalize(item["title"] + " " + item["summary"])

    severe, high, medium, low = (matches(rx, text_n) for rx in (SEVERE_RX, HIGH_RX, MEDIUM_RX, LOW_RX))
    milestones = matches(MILESTONE_RX, NEGATED.sub(" ", title_n))
    player = pick_injured_player(
        find_players(title_n, item["league"], item["title"]), title_n
    ) or find_player(text_n, item["league"])
    if not (severe or high or medium or low or (milestones and player)):
        return None

    positive = matches(POSITIVE_RX, NEGATED.sub(" ", title_n))
    noise = matches(NOISE_RX, title_n)
    league = player["league"] if player else item["league"]

    # Severity (0-25)
    if severe:
        sig = 25
    elif high:
        sig = 18
    elif medium:
        sig = 12
    else:
        sig = 5
    if positive and not severe:
        sig = min(sig, 8)  # good-news story, e.g. "cleared to return"

    # Content potential (0-15)
    content = {25: 8, 18: 5, 12: 3}.get(sig, 0)
    content += sum(pts for _, rx, pts in MECHANISM_RX if rx.search(text_n))
    content = min(content, 15)

    if player is None and noise:
        return None  # generic roundup / betting piece with no star attached

    return {
        "player": player["name"] if player else None,
        "tier": player["tier"] if player else None,
        "league": league,
        "team": (roster_team(player["name"], league) if player else None)
                or (player or {}).get("team") or find_team(title_n, league),
        "is_qb": any(rx.search(title_n) for rx in QB_RX),
        "is_star": bool(STAR_RX.search(title_n)),
        "terms": sorted(set(severe + high)),
        "milestones": milestones,
        "significance": sig,
        "content": content,
        "noise": bool(noise),
    }


def pick_injured_player(players, title_n: str):
    """With several players in one headline ("LeBron scores 40 as Suggs exits
    with knee injury"), pick the one named closest to the injury wording."""
    if len(players) <= 1:
        return players[0] if players else None
    term_pos = [m.start() for rx_list in (SEVERE_RX, HIGH_RX, MEDIUM_RX)
                for _, rx in rx_list for m in rx.finditer(title_n)]
    if not term_pos:
        return players[0]

    def name_pos(p):
        full = normalize(p["name"])
        i = title_n.find(full)
        if i < 0:
            i = title_n.find(full.split()[-1])
        return i

    def distance(p):
        i = name_pos(p)
        return min(abs(i - t) for t in term_pos) if i >= 0 else 10**6

    return min(players, key=distance)  # stable: ties keep tier order


def popularity_points(story) -> int:
    if story.get("tier"):
        return TIER_POINTS[story["tier"]]
    if story["league"] == "nfl" and story.get("is_qb"):
        return 14
    if story.get("is_star"):
        return 12
    return 6


def momentum_points(n_sources: int) -> int:
    return {0: 0, 1: 10, 2: 18, 3: 24}.get(n_sources, 30)


def recent_sources(story) -> int:
    """Outlets reporting in the last few hours. Older coverage of a long-running
    storyline doesn't count, so buzz reflects what's happening now."""
    cutoff = NOW - timedelta(hours=SETTINGS["buzz_window_hours"])
    seen = story.get("source_seen")
    if not seen:
        return len(story["sources"])
    return sum(1 for t in seen.values() if parse_iso(t) > cutoff)


def record_buzz(story):
    """Remember the most attention a story has had, and when."""
    pts = momentum_points(recent_sources(story))
    if pts >= story.get("peak_buzz", 0):
        story["peak_buzz"] = pts
        story["peak_at"] = iso(NOW)


def heat_points(story) -> int:
    """A story that was heavily covered recently stays 'warm': its peak buzz
    fades linearly to zero over storyline_heat_hours instead of vanishing
    after a few hours. So a follow-up (e.g. the MRI result the next day) on a
    story everyone was talking about last night still scores like a big story."""
    if not story.get("peak_at"):
        return 0
    hours = (NOW - parse_iso(story["peak_at"])).total_seconds() / 3600
    fade = max(0.0, 1 - hours / SETTINGS["storyline_heat_hours"])
    return int(round(story["peak_buzz"] * fade))


def score_story(story) -> dict:
    current = momentum_points(recent_sources(story))
    heat = heat_points(story)
    parts = {
        "popularity": popularity_points(story),
        "momentum": max(current, heat),
        "carried": heat > current,
        "severity": story["significance"],
        "content": story["content"],
    }
    total = parts["popularity"] + parts["momentum"] + parts["severity"] + parts["content"]
    total *= SETTINGS["league_multiplier"].get(story["league"], 0.8)
    if story.get("noise"):
        total *= 0.6
    parts["total"] = int(round(min(total, 100)))
    return parts


# ---------------------------------------------------------------------------
# State (what we've already seen / alerted)
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        try:
            return load_json(STATE_FILE), False
        except (json.JSONDecodeError, OSError) as e:
            print(f"State file unreadable ({e}); starting fresh")
    return {"version": 1, "seen": {}, "stories": {}}, True


def save_state(state):
    cutoff = NOW - timedelta(hours=72)
    storyline_cutoff = NOW - timedelta(days=SETTINGS["storyline_window_days"])
    state["seen"] = {k: v for k, v in state["seen"].items() if parse_iso(v) > cutoff}
    state["stories"] = {
        k: v for k, v in state["stories"].items()
        if parse_iso(v["last_seen"]) > (storyline_cutoff if v.get("player") else cutoff)
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def find_story(state, item_tokens, info):
    window = timedelta(hours=SETTINGS["story_merge_window_hours"])
    storyline_window = timedelta(days=SETTINGS["storyline_window_days"])
    best, best_sim = None, 0.0
    for sid, s in state["stories"].items():
        age = NOW - parse_iso(s["last_seen"])
        if info["player"] and s.get("player"):
            # Named star: everything about them within the storyline window is
            # one ongoing storyline (blood clot -> pulmonary embolism -> return).
            if info["player"] == s["player"] and age <= storyline_window:
                return sid
            continue
        if age > window:
            continue
        if s["league"] != info["league"]:
            continue
        sim = jaccard(item_tokens, set(s["tokens"]))
        shared = len(item_tokens & set(s["tokens"]))
        if sim >= 0.5 and shared >= 2 and sim > best_sim:
            best, best_sim = sid, sim
    return best


def merge_item(state, item, info):
    item_tokens = tokens(item["title"])
    sid = find_story(state, item_tokens, info)
    if sid is None:
        sid = short_hash(f"{info['league']}|{info['player'] or item['title']}|{iso(NOW)}")
        state["stories"][sid] = {
            "player": info["player"], "tier": info["tier"], "league": info["league"],
            "team": info["team"], "is_qb": info["is_qb"], "is_star": info["is_star"],
            "headline": item["title"], "tokens": sorted(item_tokens), "terms": [],
            "significance": 0, "content": 0, "noise": info["noise"],
            "sources": [], "links": [], "first_seen": iso(NOW), "last_seen": iso(NOW),
            "alerted": False, "alerted_score": 0, "alerted_terms": [], "updates_sent": 0,
            "milestones": [], "latest_headline": item["title"], "last_alert": None,
        }
    s = state["stories"][sid]
    s["last_seen"] = iso(NOW)
    s["team"] = s["team"] or info["team"]
    s["is_qb"] = s["is_qb"] or info["is_qb"]
    s["is_star"] = s["is_star"] or info["is_star"]
    s["noise"] = s["noise"] and info["noise"]  # one real report clears the noise flag
    s["terms"] = sorted(set(s["terms"]) | set(info["terms"]))
    s["milestones"] = sorted(set(s.get("milestones", [])) | set(info["milestones"]))
    s["latest_headline"] = item["title"]
    if info["significance"] >= s["significance"]:
        s["significance"] = info["significance"]
        s["headline"] = item["title"]  # newest headline with the most serious wording
    s["content"] = max(s["content"], info["content"])
    if item["source"] not in s["sources"]:
        s["sources"].append(item["source"])
    s.setdefault("source_seen", {})[item["source"]] = iso(NOW)
    record_buzz(s)
    # Keep the 5 most recent links, newest first, so updates link to the new news.
    s["links"] = [{"source": item["source"], "url": item["link"]}] + [
        l for l in s["links"] if l["source"] != item["source"]
    ][:4]
    s["tokens"] = sorted(set(s["tokens"]) | item_tokens)[:60]
    return sid


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

LEAGUE_LABEL = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL", "soccer": "Soccer"}


IN_GAME = {
    "carted off", "carted", "stretcher", "stretchered", "left the game", "leaves game",
    "leaves the game", "left game", "exits game", "exits", "exited", "locker room",
    "helped off", "limped off", "limps off", "went down", "goes down", "down on the field",
    "down on the court", "won't return", "will not return", "left practice",
    "leaves practice", "exits practice", "collapsed on",
}


def recommendation(s, total: int, kind: str) -> str:
    on_video = bool(IN_GAME & set(s["terms"]))
    if kind == "UPDATE":
        return "📌 Storyline update. Possible follow-up video on what this changes for recovery."
    if total >= SETTINGS["high_priority_threshold"]:
        return ("🎬 Strong TikTok candidate. Find the replay and film early." if on_video
                else "🎬 Strong TikTok candidate. Big medical/status news; explain what it means.")
    if total >= SETTINGS["alert_threshold"]:
        return ("👀 Worth a look. Check the replay before committing." if on_video
                else "👀 Worth a look. Read the report before committing.")
    return "Low priority."


def format_alert(s, parts, kind: str, new_terms=None, playing: bool = False) -> str:
    def e(text, quote=False):
        return html.escape(text, quote=quote)

    total = parts["total"]
    high = total >= SETTINGS["high_priority_threshold"]
    if kind == "UPDATE":
        header = f"🔁 <b>UPDATE · {total}/100</b>"
    elif high:
        header = f"🚨 <b>HIGH PRIORITY · {total}/100</b>"
    else:
        header = f"⚠️ <b>Injury alert · {total}/100</b>"

    who = s["player"] or "Unidentified player"
    line2 = " · ".join(x for x in (who, s.get("team"), LEAGUE_LABEL.get(s["league"], s["league"].upper())) if x)
    headline = s.get("latest_headline", s["headline"]) if kind == "UPDATE" else s["headline"]
    lines = [header, f"<b>{e(line2)}</b>", f"<i>{e(headline)}</i>", ""]
    first = parse_iso(s["first_seen"])
    if kind == "UPDATE" and NOW - first > timedelta(hours=24):
        days = (NOW - first).days
        lines.insert(1, f"<i>Ongoing storyline, first flagged {first.strftime('%b')} {first.day} "
                        f"({days} day{'s' if days != 1 else ''} ago)</i>")

    if playing:
        lines.append(f"🔴 {e(s['team'])} game is on now / just ended")
    if new_terms:
        lines.append(f"New info: {e(', '.join(drop_subphrases(new_terms)))}")
    if s["terms"]:
        lines.append(f"Signals: {e(', '.join(drop_subphrases(s['terms'])[:6]))}")
    n = recent_sources(s)
    lines.append(f"{n} outlet{'s' if n != 1 else ''} reporting in the last "
                 f"{SETTINGS['buzz_window_hours']}h")
    lines.append(recommendation(s, total, kind))
    lines.append(
        f"<code>pop {parts['popularity']} · buzz {parts['momentum']}"
        f"{' (carried over)' if parts.get('carried') else ''} · "
        f"severity {parts['severity']} · content {parts['content']}</code>"
    )
    lines.append("")
    for link in s["links"][:4]:
        lines.append(f'• <a href="{e(link["url"], quote=True)}">{e(link["source"])}</a>')
    return "\n".join(lines)


def send_telegram(text: str, dry_run: bool) -> bool:
    if dry_run:
        print("\n----- ALERT (dry run) -----\n" + re.sub(r"<[^>]+>", "", text) + "\n---------------------------")
        return True
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if resp.ok:
            return True
        # Never print the token; the response body doesn't contain it.
        print(f"  ! Telegram error {resp.status_code}: {resp.text[:300]}")
    except requests.RequestException as e:
        print(f"  ! Telegram request failed: {type(e).__name__}")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_items(state, items) -> set:
    """Record new headlines and merge them into stories. Returns story ids touched."""
    max_age = timedelta(hours=SETTINGS["max_story_age_hours"])
    touched = set()
    new_items = 0
    for item in items:
        key = short_hash(normalize(item["title"]) + "|" + normalize(item["source"]))
        if key in state["seen"]:
            continue
        state["seen"][key] = iso(NOW)
        if NOW - item["published"] > max_age:
            continue
        info = analyze(item)
        if info is None:
            continue
        new_items += 1
        sid = merge_item(state, item, info)
        touched.add(sid)
        touched |= credit_other_players(state, item, info, sid)
    print(f"{new_items} new injury headlines, {len(touched)} stories touched")
    return touched


def credit_other_players(state, item, info, primary_sid) -> set:
    """A headline like "Jalen Williams, Jalen Suggs injuries..." is coverage of
    both players. Credit the extra coverage (buzz only, no injury details) to
    other players who already have an active storyline."""
    others = {p["name"] for p in find_players(normalize(item["title"]), info["league"], item["title"])}
    others.discard(info["player"])
    window = timedelta(days=SETTINGS["storyline_window_days"])
    credited = set()
    for sid, s in state["stories"].items():
        if (sid != primary_sid and s.get("player") in others
                and NOW - parse_iso(s["last_seen"]) <= window):
            if item["source"] not in s["sources"]:
                s["sources"].append(item["source"])
            s.setdefault("source_seen", {})[item["source"]] = iso(NOW)
            s["last_seen"] = iso(NOW)
            record_buzz(s)
            credited.add(sid)
    return credited


def send_alerts(state, touched, live_teams, dry_run: bool):
    threshold = SETTINGS["alert_threshold"]
    high = SETTINGS["high_priority_threshold"]
    alerts = []
    for sid in touched:
        s = state["stories"][sid]
        parts = score_story(s)
        print(f"  {parts['total']:>3}  {s['league']:<6} {s['player'] or '-':<24} {s['headline'][:80]}")
        if not s["alerted"]:
            if parts["total"] >= threshold:
                alerts.append((sid, parts, "NEW", None))
        elif s["updates_sent"] < SETTINGS["max_updates_per_story"] and (
                s.get("notified") or parts["total"] >= threshold):
            # Once you've been alerted about a story, every real development
            # gets an update, even after the initial buzz has died down.
            already = set(s["alerted_terms"])
            new_all = [t for t in s["terms"] + s.get("milestones", [])
                       if not any(t in a or a in t for a in already)]
            new_severe = [t for t in new_all if t in SEVERE]
            became_high = s["alerted_score"] < high <= parts["total"]
            last = s.get("last_alert")
            cooled = last is None or NOW - parse_iso(last) >= timedelta(
                minutes=SETTINGS["update_cooldown_minutes"])
            # Serious new detail or a jump to high priority: send right away.
            # Other new detail (left game, won't return, MRI, cleared, activated):
            # send if we haven't messaged about this story in the last little while.
            if new_severe or became_high or (new_all and cooled):
                alerts.append((sid, parts, "UPDATE", new_all))

    alerts.sort(key=lambda a: -a[1]["total"])
    for sid, parts, kind, new_terms in alerts:
        s = state["stories"][sid]
        playing = bool(s.get("team")) and s["team"] in live_teams.get(s["league"], set())
        if send_telegram(format_alert(s, parts, kind, new_terms, playing), dry_run):
            if kind == "UPDATE":
                s["updates_sent"] += 1
            s.update(alerted=True, notified=True, alerted_score=parts["total"], last_alert=iso(NOW),
                     alerted_terms=sorted(set(s["terms"]) | set(s.get("milestones", []))))
        # If sending failed, the story stays un-alerted and is retried next run.
    print(f"Sent {len(alerts)} alert(s)")


def tick():
    """Advance the clock used for timestamps (live mode runs for several minutes)."""
    global NOW
    NOW = datetime.now(timezone.utc)


def run(dry_run: bool):
    state, fresh_start = load_state()
    load_rosters()
    live = detect_live_games()
    live_teams = {lg: info["teams"] for lg, info in live.items()}
    if live:
        print("LIVE: " + "; ".join(f"{lg.upper()} ({info['why']})" for lg, info in live.items()))

    # Overnight with no games: a full sweep every 15 minutes is plenty.
    quiet = SETTINGS.get("quiet_hours", {})
    hour_et = NOW.astimezone(EASTERN).hour
    last = state.get("last_sweep")
    if (not live and not fresh_start and last and quiet
            and quiet["start_hour_et"] <= hour_et < quiet["end_hour_et"]
            and NOW - parse_iso(last) < timedelta(minutes=quiet["sweep_every_minutes"] - 1)):
        print(f"Quiet hours ({hour_et}:00 ET), last sweep {last}; skipping this run.")
        return

    refresh_rosters()

    # 1) Full sweep of every search and feed.
    touched = process_items(state, fetch_all())
    state["last_sweep"] = iso(NOW)

    if fresh_start:
        # First run (or lost state): record what's already out there without
        # alerting, otherwise you'd get a flood of old stories at once.
        for sid in touched:
            s = state["stories"][sid]
            parts = score_story(s)
            s.update(alerted=True, alerted_score=parts["total"], last_alert=iso(NOW),
                     alerted_terms=sorted(set(s["terms"]) | set(s.get("milestones", []))))
        save_state(state)
        msg = (
            "✅ <b>Injury monitor is live.</b>\n"
            f"Baseline recorded ({len(touched)} current stories). "
            "You'll be alerted about new injuries from here on."
        )
        send_telegram(msg, dry_run)
        print("First run: baseline saved, no alerts sent.")
        return

    send_alerts(state, touched, live_teams, dry_run)
    save_state(state)

    # 2) Live mode: games are on, so keep re-checking the in-game searches for
    #    the live leagues every ~60s until the next scheduled run takes over.
    lm = SETTINGS.get("live_mode", {})
    if not (live and lm.get("enabled")):
        return
    started = datetime.now(timezone.utc)
    while (datetime.now(timezone.utc) - started).total_seconds() + lm["poll_seconds"] <= lm["loop_seconds"]:
        time.sleep(lm["poll_seconds"])
        tick()
        print(f"\n-- live re-check {NOW.astimezone(EASTERN):%H:%M:%S} ET --")
        touched = process_items(state, fetch_live(list(live)))
        send_alerts(state, touched, live_teams, dry_run)
        save_state(state)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="send a test Telegram message and exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    args = ap.parse_args()

    if not args.dry_run:
        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(k)]
        if missing:
            sys.exit(
                f"Missing secret(s): {', '.join(missing)}. Add them in GitHub under "
                "Settings > Secrets and variables > Actions, or run with --dry-run."
            )

    if args.test:
        ok = send_telegram(
            "🧪 <b>Test alert from The Injury Analyst monitor.</b>\n"
            "If you can read this, Telegram is connected correctly.",
            args.dry_run,
        )
        sys.exit(0 if ok else 1)

    run(args.dry_run)


if __name__ == "__main__":
    main()
