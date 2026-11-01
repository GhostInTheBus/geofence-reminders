---
name: location-based-reminders
description: Location reminders ("remind me when I get to X to do Y"). Arms a geofence on the phone's Dawarich location and pings on arrival.
version: 1.0.0
author: GhostInTheBus
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [location, reminders, geofence, dawarich, telegram, cron]
    category: productivity
---

# Location-Based Reminders

The user texts a reminder in plain language ("remind me when I get to the hardware store to buy hinges"). You geocode the place, add ONE entry with the helper script, and reply with ONE confirmation line. Nothing else is needed: the poller picks it up by itself and pings the user when their phone arrives.

Everything goes through one script. Never hand-edit a registry file.

```
S=~/.hermes/scripts/geofence_poll.py
```

## When to Use

The user says "remind me when I get to X to do Y", "when I'm at X remind me to...", "next time I'm at X...", hands you a day plan with stops, or asks to list, cancel or debug location reminders.

## First-time setup (the user runs it, not you)

If `$S` does not exist, setup has not been run. Tell the user to run `scripts/geofence_setup.sh` from this skill's folder in a terminal on this machine:

```
bash "$(find ~/.hermes/skills -path '*location-based-reminders/scripts/geofence_setup.sh' | head -1)"
```

It asks for the Dawarich URL, the Dawarich API key (hidden prompt) and the Hermes delivery target (for example `telegram:<chat id>`), copies `scripts/geofence_poll.py` into `~/.hermes/scripts/`, runs two checks and creates the `geofence-poller` cron job. Never run it yourself: it reads a secret from a hidden prompt.

## Intake (the whole job, one reply)

1. **Split the message** into PLACE and TASK. Keep TASK in the user's own words, trimmed to an imperative ("Buy cabinet hinges"). Do not rewrite or embellish it. It is sent back verbatim.
2. **Geocode PLACE**
   - Specific place with a city: `python3 $S --geocode "Union Station, Denver CO"`
   - Chain or generic place with no city ("the hardware store", "the post office"): `python3 $S --geocode "post office" --near here` (searches around the phone's last known position, nearest match first).
   - A town ("when I get to Boulder"): `python3 $S --geocode "Boulder, CO"`
   - Output lines look like `GEOCODE <lat>,<lon> | <category>/<type> | <display name> | 3.2 km away`. Take the first sane one.
   - `GEOCODE none`, or two plausible matches far apart: ask ONE short question ("Which one, Boulder or Denver?"). Do not guess between cities.
3. **Pick the radius (km)**. Always pass `--radius`. Too big means the ping lands before the user is there.
   - 0.5 for a house or an exact street address
   - 1 for a store, post office, shop or anything with a parking lot
   - 5 for a neighborhood or campus
   - 15 for "when I get to <town>"
4. **Add it**
   ```
   python3 $S --add --name "Hardware store (Main St)" --lat <lat> --lon <lon> --radius 1 --task "Buy cabinet hinges"
   ```
   Optional `--days N` (default 7, then it expires and is garbage collected).
5. **Reply with the first line the script printed, and nothing else.** It looks like:
   `✅ Watching Hardware store (Main St), 1 km, expires Nov 8. Ping lands a few minutes after you arrive.`
   - `ALREADY_WATCHING <id>`: the same place and task is already active. Say it is already set.
   - `NAS_DOWN ... nothing was changed` or `ADD_ERROR ...`: the reminder was NOT saved. Say that plainly in one line. The user asked for something and it did not happen, so this is not noise.

## Day plans (several stops)

When the user gives you a plan for the day, or points you at one (a note with stops and addresses), **arm every stop that has a place, right away, in one go.** Do not ask "want me to set a geofence for X?". The point of this skill is that the user never has to ask. Asking is the failure.

- One `--add` per stop. TASK = what the user has to do there, taken from the plan in their words. Add `--days 1`.
- Chain them in ONE terminal call: `python3 $S --add ... ; python3 $S --add ... ; python3 $S --list`
- Skip stops with no findable place, and say which ones you skipped in the same reply.
- Reply with a short list: one line per armed stop (name, radius). Nothing else.
- When the plan changes ("skip the bank", "the pickup is now a delivery"), `--remove` or re-add the affected stop in the same turn.
- A stop the user is at right now, or reached within the last 90 minutes, still fires. No special handling needed.

## Speed

The user is usually driving or standing in a parking lot. A reply that takes minutes is a miss.

- Intake is geocode, add, reply. No reverse geocoding, no extra lookups, no research, no commentary.
- "Where am I" is `python3 $S --whereami` and nothing else. Give the coordinates and the age. Do not call other geocoding services to name the spot.
- "Send me a test" is `python3 $S --test-ping`. Never build a custom test geofence.
- If `--geocode` times out once, try once more with the street address, then ask. Do not loop through other APIs.

## Other requests

| User says | Run |
|---|---|
| "what location reminders do I have" | `python3 $S --list` |
| "cancel the hardware store one" | `python3 $S --list`, then `python3 $S --remove <id>` |
| "I already did that one" | `python3 $S --mark-fired <id>` |
| "is the location thing working?" | `python3 $S --selftest` (Dawarich reachability and when the phone last reported) |
| "send me a test" | `python3 $S --test-ping` (arrives on the next 10 min tick) |
| "where does it think I am" | `python3 $S --whereami` |

## How it works (so you can answer questions)

- **Local mode (default):** the Hermes cron job `geofence-poller` runs `$S` every 10 minutes and relays the message. Reminders only fire while this machine is awake and the Hermes gateway is running. `python3 $S --nas-health` prints `NAS OFF`.
- **NAS mode (optional):** an always on box next to Dawarich (a NAS or any Docker host) runs `scripts/geofence_nas.py` in a container (`templates/docker-compose.yml`, installed by `scripts/nas_install.sh`). It polls every 5 minutes and sends through the Telegram Bot API itself, so reminders fire with this machine asleep. It is switched on by `bash ~/.hermes/scripts/geofence_setup.sh nas`, which creates `~/.hermes/secrets/geofence_nas.env`; `nas-off` rolls back. Only say the NAS is doing it when `--nas-health` prints `NAS OK`.
- It fires when either is true:
  - a GPS point inside the radius is under 30 minutes old and newer than the reminder, or
  - the phone's LAST KNOWN position is inside the radius (up to 3 hours old). That covers late uploads, and it covers "they got there before the reminder was set" as long as they arrived no more than 90 minutes before and have been there at least 20 minutes.
- What arrives: `📍 <place name> — <task>` (place name in bold).
- One shot. A fired reminder never fires again. If the user wants it again, add a new one.
- The cron job never wakes the LLM on a quiet tick (the script ends with `{"wakeAgent": false}`). The LLM runs only to relay a fired reminder or the feed warning below.
- **Feed watchdog:** every tick checks when the phone last reported. If the newest Dawarich point is older than 24 h (`FEED_STALE_H` in the script) it sends ONE warning (`⚠️ Location feed is stale ...`) and stays quiet until the feed recovers, then re-arms. State: `~/.hermes/geofence_feed_state.json`. 24 h because a parked phone can be silent all night.

## When the user says "it's not working"

Do not speculate and do not suggest other tools. Run ONE command:

```
python3 $S --list; python3 $S --selftest
```

Then answer in three short lines: (1) what is armed right now, (2) when the phone last reported, (3) the most likely reason the thing they expected did not fire. The usual reason is the plain one: **no reminder was armed for that place.** If so, say that, arm it now, and arm the rest of their plan too.

## Hard rules

- NEVER run `geofence_poll.py` with no arguments by hand. That is the cron path. Use `--dry-run` or `--nas-health`.
- NEVER edit, pause or recreate the `geofence-poller` cron job as part of intake.
- NEVER re-arm a fired entry.
- NEVER print, log or write the Dawarich key, the Telegram bot token or the NAS API token anywhere. They live only in `~/.hermes/secrets/` (and in `geofence.env` next to the container when NAS mode is used).
- Fail silent. If Dawarich or the phone is down, reminders just pause. Do not message the user about it unless they asked whether it is working. (A failed `--add` is different, see above.)

## Pitfalls

- **`&` in the task text can block the terminal call:** some terminal harnesses treat a literal `&` as backgrounding and refuse the command even inside quotes ("Acme Pipe & Supply"). Rephrase without it ("Acme Pipe Supply") and re-run.
- **Already there:** a reminder set for the place the user is at fires on the next check, once they have been there 20 minutes. Past 90 minutes it is treated as "next time".
- **Just left:** "next time I'm at the store" sent while driving away does not fire, because the newest point is already outside the radius.
- **Phone trackers usually log only while moving.** When the user is parked, the newest point just gets older. `newest point 45m ago` while they sit somewhere is NORMAL. It is not lag, not a battery setting, and not a Dawarich problem. Do not tell them it is.
- **Phone really not reporting:** only if `--selftest` shows the phone last reported many hours ago on a day they have been moving around. Over 24 h the feed warning goes out automatically.
- **Phones can upload in batches.** A point can reach Dawarich 10 to 20 minutes late. That is why the window is 30 minutes.
- **Precision:** fine for "I'm at the store", not for "I just pulled into the driveway".
- **Machine off or asleep (local mode):** nothing fires. Armed reminders wait and fire on the first tick after it is back, if the user is still inside the radius.
- **Nominatim (OpenStreetMap) is sparse for some shops.** If `--geocode` finds nothing, try the street address, or ask for the city or a nearby cross street.

## Files in this skill

- `scripts/geofence_poll.py`: poller and intake helpers (setup copies it to `~/.hermes/scripts/`)
- `scripts/geofence_setup.sh`: setup, health checks (`diag`, `down`), NAS deploy (`nas`) and rollback (`nas-off`)
- `scripts/geofence_nas.py`, `scripts/nas_install.sh`, `templates/docker-compose.yml`: the optional always on NAS service
- `templates/dawarich.env.example`, `templates/geofence.conf.example`, `templates/geofence.env.example`: what the config files look like

## Verification

`python3 $S --selftest` prints `SELFTEST OK` and the age of the newest point. `python3 $S --test-ping` delivers one test message on the next tick.
