# Injury Analyst Alerts

Automated sports injury monitoring for **The Injury Analyst**. GitHub runs `monitor.py` in the cloud every 5 minutes, checking about every minute while games are live. It reads Google News and ESPN injury headlines, groups reports about the same injury, scores each injury 0–100, and sends a Telegram alert to your phone when one is worth a look. Your computer does not need to be on.

## How alerts work

| Score | What you get |
|---|---|
| 80–100 | 🚨 **High priority**: strong TikTok candidate |
| 60–79 | ⚠️ **Injury alert**: worth checking the replay |
| below 60 | Recorded, no alert (planned: daily digest) |

**Score = popularity (30) + buzz (30) + severity (25) + content potential (15)**, then multiplied by a league weight (NFL/NBA 1.0, MLB 0.85, NHL/soccer 0.8).

- **Popularity**: is the player on the star list in `config/players.json`? Tier 1 = 30, tier 2 = 22, tier 3 = 15. Unlisted NFL quarterbacks get 14, anyone else 6.
- **Buzz**: how many different outlets are reporting it (1 = 10, 2 = 18, 3 = 24, 4+ = 30). This is a free stand-in for X/Twitter engagement.
- **Severity**: worst wording seen. *Torn, carted off, fracture, season-ending* = 25. *Won't return, locker room, MRI, concussion* = 18. General injury mentions = 12. Routine status = 5. Good news like *cleared* or *no structural damage* is capped at 8.
- **Content potential**: severity plus visible-mechanism words such as *non-contact, awkward, landed, video, replay*.

**No duplicate spam.** Five outlets reporting the same injury become one story. You get **one alert**, then **🔁 UPDATE** messages only when something genuinely new is reported.

**Storylines.** Everything about a listed star is tracked as one ongoing storyline for 30 days. Example: *Hurts held out with blood clot* (alert) → routine "still monitoring" articles (silent) → *clots progressed to pulmonary embolism* a week later (update, labeled with when the storyline started) → *cleared to return* (update). Updates fire on:
- a new serious detail (torn, surgery, fracture, embolism, season-ending…): immediately
- the story jumping to high priority because many outlets are suddenly covering it: immediately
- other new details (left the game, won't return, MRI, diagnosed, setback, cleared, activated…): at most one per 30 minutes per story
- maximum 8 updates per storyline

**What's covered:** in-game injuries, practice injuries, players held out before games, and medical/illness news (blood clots, cardiac, hospitalizations), not just orthopedic injuries. Routine rest days and "load management" are scored low.

**Buzz is measured over the last 6 hours**, so an old storyline only scores high again when coverage spikes again.

Fantasy, betting, and generic "injury report" roundups are ignored unless they name a star. Headlines older than 3 hours are ignored.

## How often it checks

| When | How often |
|---|---|
| A game in that league is **live** (or starts within 90 min, or ended within the last hour) | Full sweep every 5 min **plus** in-game searches for that league about **every 60 seconds** |
| Normal hours, no games | Full sweep every 5 min |
| Overnight 2–7 AM ET, no games | Full sweep every 15 min |

"Live" comes from ESPN's public scoreboard, so it automatically follows the real schedule: Sunday/Monday/Thursday NFL, Saturday games late in the season, London and holiday games, nightly NBA, playoffs, and off-seasons. If the scoreboard can't be reached, the monitor falls back to normal 5-minute sweeps. Alerts about a team that's playing right now are tagged 🔴.

GitHub's scheduler can't trigger more often than every 5 minutes. The 60-second checks happen *inside* each run while games are on, which is free on a public repo.

## Customizing (no coding needed)

Edit these files directly on GitHub (open the file → pencil icon → **Commit changes**):

- `config/players.json`: add, remove, or re-tier players. Keep the JSON punctuation intact (quotes and commas).
- `config/settings.json`: alert thresholds, league weights, and the news searches that are run.
- `config/teams.json`: team names used to label alerts.

## Turning it on and off

- **Off:** Settings → Secrets and variables → Actions → **Variables** tab → set `MONITOR_ENABLED` to `false` (or delete it).
- **On:** same place, `MONITOR_ENABLED` = `true`.
- **Run once now / send a test message:** Actions tab → **Injury monitor** → **Run workflow** (tick *Only send a test Telegram message* for a test).

## Setup checklist

1. Telegram bot created with BotFather. Keep the token private.
2. Repository secrets (Settings → Secrets and variables → Actions → **Secrets** → New repository secret):
   - `TELEGRAM_BOT_TOKEN`: the token from BotFather
   - `TELEGRAM_CHAT_ID`: your numeric chat ID
3. Manual test run with *Only send a test Telegram message* ticked → test message arrives.
4. Manual normal run → "Injury monitor is live" arrives. This first run records current stories without alerting about them.
5. Repository variable `MONITOR_ENABLED` = `true` → automatic checks every 5 minutes.

## Troubleshooting

- **Run fails with "Missing secret(s)"**: a secret name is misspelled or missing. Names must match exactly.
- **Telegram error 401**: the bot token is wrong. Regenerate it in BotFather and update the secret.
- **Telegram error 400 "chat not found"**: the chat ID is wrong, or you haven't sent your bot a message yet. Open the bot in Telegram and press **Start**.
- **Runs are late**: GitHub doesn't guarantee schedule timing and may delay runs when it's busy. Typical delay is a few minutes. It's occasionally longer.
- **Too many / too few alerts**: raise or lower `alert_threshold` in `config/settings.json`, or adjust player tiers.

## Free-tier notes

- GitHub Actions is free and unlimited on **public** repositories. **Private** repositories on the Free plan get 2,000 minutes/month. A 5-minute schedule uses roughly 8,600 run-minutes/month, so a private repo would run out in about a week, after which runs stop until the next month. No charge applies unless you add a payment method.
- On public repositories, GitHub pauses schedules after 60 days with no commits. Any small commit (even editing this README) resets the clock.
- No secrets are ever stored in the code. Telegram credentials live only in encrypted GitHub secrets.

## Roadmap

- **Phase 2:** daily digest of sub-60 stories, live-game exit detection, diagnosis-vs-update tracking, latency measurement.
- **Phase 3:** X/Grok engagement data, viral scoring, TikTok hook suggestions, learning from video performance.
