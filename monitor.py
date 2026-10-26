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
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
STATE_FILE = ROOT / "state" / "state.json"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (injury-analyst-alerts; +https://github.com)"}
NOW = datetime.now(timezone.utc)

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
]
MEDIUM = [  # 12 pts: injury mentioned, severity unclear
    "questionable to return", "doubtful to return", "being evaluated", "evaluated",
    "medical tent", "blue tent", "injured", "injury", "injuries", "injures", "hurt",
    "hamstring", "ankle", "knee", "shoulder", "groin", "calf", "foot", "hip", "wrist",
    "elbow", "oblique", "quad", "back spasms", "sprain", "sprained", "strain", "strained",
    "bruise", "contusion", "illness",
]
LOW = [  # 5 pts: routine status news
    "injury report", "day-to-day", "limited practice", "did not practice", "dnp",
    "questionable", "doubtful", "probable", "load management",
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
TIER_POINTS = {"tier1": 30, "tier2": 22, "tier3": 15}


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
                index.append({
                    "league": league,
                    "tier": tier,
                    "name": names[0],
                    "patterns": [phrase_pattern(n) for n in names],
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


def find_player(text: str, feed_league: str):
    """Best player match: prefer the feed's league, then higher tier."""
    hits = [p for p in PLAYERS if any(rx.search(text) for rx in p["patterns"])]
    if not hits:
        return None
    hits.sort(key=lambda p: (p["league"] != feed_league, p["tier"]))
    return hits[0]


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
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=15)
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
    items = []
    for url, league, is_google in jobs:
        items += fetch_feed(url, league, is_google)
    print(f"Fetched {len(items)} headlines from {len(jobs)} feeds")
    return items


# ---------------------------------------------------------------------------
# Analysis and scoring
# ---------------------------------------------------------------------------

def analyze(item: dict):
    """Return analysis dict, or None if this isn't an injury story."""
    title_n = normalize(item["title"])
    text_n = normalize(item["title"] + " " + item["summary"])

    severe, high, medium, low = (matches(rx, text_n) for rx in (SEVERE_RX, HIGH_RX, MEDIUM_RX, LOW_RX))
    if not (severe or high or medium or low):
        return None

    positive = matches(POSITIVE_RX, NEGATED.sub(" ", title_n))
    noise = matches(NOISE_RX, title_n)
    player = find_player(title_n, item["league"]) or find_player(text_n, item["league"])
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
        "team": find_team(title_n, league),
        "is_qb": any(rx.search(title_n) for rx in QB_RX),
        "is_star": bool(STAR_RX.search(title_n)),
        "terms": sorted(set(severe + high)),
        "significance": sig,
        "content": content,
        "noise": bool(noise),
    }


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


def score_story(story) -> dict:
    parts = {
        "popularity": popularity_points(story),
        "momentum": momentum_points(len(story["sources"])),
        "severity": story["significance"],
        "content": story["content"],
    }
    total = sum(parts.values())
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
    state["seen"] = {k: v for k, v in state["seen"].items() if parse_iso(v) > cutoff}
    state["stories"] = {
        k: v for k, v in state["stories"].items() if parse_iso(v["last_seen"]) > cutoff
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def find_story(state, item_tokens, info):
    window = timedelta(hours=SETTINGS["story_merge_window_hours"])
    best, best_sim = None, 0.0
    for sid, s in state["stories"].items():
        if NOW - parse_iso(s["last_seen"]) > window:
            continue
        if info["player"] and s.get("player"):
            if info["player"] == s["player"]:
                return sid
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
        }
    s = state["stories"][sid]
    s["last_seen"] = iso(NOW)
    s["team"] = s["team"] or info["team"]
    s["is_qb"] = s["is_qb"] or info["is_qb"]
    s["is_star"] = s["is_star"] or info["is_star"]
    s["noise"] = s["noise"] and info["noise"]  # one real report clears the noise flag
    s["terms"] = sorted(set(s["terms"]) | set(info["terms"]))
    if info["significance"] >= s["significance"]:
        s["significance"] = info["significance"]
        s["headline"] = item["title"]  # newest headline with the most serious wording
    s["content"] = max(s["content"], info["content"])
    if item["source"] not in s["sources"]:
        s["sources"].append(item["source"])
        if len(s["links"]) < 5:
            s["links"].append({"source": item["source"], "url": item["link"]})
    s["tokens"] = sorted(set(s["tokens"]) | item_tokens)[:60]
    return sid


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

LEAGUE_LABEL = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB", "nhl": "NHL", "soccer": "Soccer"}


def recommendation(total: int) -> str:
    if total >= SETTINGS["high_priority_threshold"]:
        return "🎬 Strong TikTok candidate. Find the replay and film early."
    if total >= SETTINGS["alert_threshold"]:
        return "👀 Worth a look. Check the replay before committing."
    return "Low priority."


def format_alert(s, parts, kind: str, new_terms=None) -> str:
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
    lines = [header, f"<b>{e(line2)}</b>", f"<i>{e(s['headline'])}</i>", ""]

    if new_terms:
        lines.append(f"New info: {e(', '.join(drop_subphrases(new_terms)))}")
    if s["terms"]:
        lines.append(f"Signals: {e(', '.join(drop_subphrases(s['terms'])[:6]))}")
    n = len(s["sources"])
    lines.append(f"Reported by {n} outlet{'s' if n != 1 else ''}")
    lines.append(recommendation(total))
    lines.append(
        f"<code>pop {parts['popularity']} · buzz {parts['momentum']} · "
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

def run(dry_run: bool):
    state, fresh_start = load_state()
    max_age = timedelta(hours=SETTINGS["max_story_age_hours"])

    touched = set()
    new_items = 0
    for item in fetch_all():
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
        touched.add(merge_item(state, item, info))

    print(f"{new_items} new injury headlines, {len(touched)} stories touched")

    if fresh_start:
        # First run (or lost state): record what's already out there without
        # alerting, otherwise you'd get a flood of old stories at once.
        for sid in touched:
            s = state["stories"][sid]
            parts = score_story(s)
            s.update(alerted=True, alerted_score=parts["total"], alerted_terms=s["terms"])
        save_state(state)
        msg = (
            "✅ <b>Injury monitor is live.</b>\n"
            f"Baseline recorded ({len(touched)} current stories). "
            "You'll be alerted about new injuries from here on."
        )
        send_telegram(msg, dry_run)
        print("First run: baseline saved, no alerts sent.")
        return

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
        elif s["updates_sent"] < SETTINGS["max_updates_per_story"]:
            new_terms = [t for t in s["terms"] if t not in s["alerted_terms"] and t in SEVERE]
            became_high = s["alerted_score"] < high <= parts["total"]
            if parts["total"] >= threshold and (became_high or new_terms):
                alerts.append((sid, parts, "UPDATE", new_terms))

    alerts.sort(key=lambda a: -a[1]["total"])
    for sid, parts, kind, new_terms in alerts:
        s = state["stories"][sid]
        if send_telegram(format_alert(s, parts, kind, new_terms), dry_run):
            if kind == "UPDATE":
                s["updates_sent"] += 1
            s.update(alerted=True, alerted_score=parts["total"], alerted_terms=s["terms"])
        # If sending failed, the story stays un-alerted and is retried next run.

    print(f"Sent {len(alerts)} alert(s)")
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
