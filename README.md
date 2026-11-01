# geofence-reminders

Location based reminders for [Hermes Agent](https://github.com/NousResearch/hermes-agent). Text your agent "remind me when I get to the hardware store to buy hinges" and a Telegram message lands when your phone gets there.

No app to open, no reminders to pre-create, nothing extra running on the phone. It reads the location your phone already sends to a self hosted [Dawarich](https://github.com/Freika/dawarich) server.

```
you:     remind me when I get to the post office to mail the package
hermes:  ✅ Watching Post Office (Main St), 1 km, expires Nov 8. Ping lands a few minutes after you arrive.

...later, when your phone pulls into the parking lot...

hermes:  📍 Post Office (Main St) — mail the package
```

## How it works

```
you text Hermes  ->  skill geocodes the place (OpenStreetMap)  ->  geofence_poll.py --add  ->  registry (JSON)
                                                                                                    |
Hermes cron job "geofence-poller", every 10 min:                                                    v
  one Dawarich /api/v1/points call  ->  distance check against every armed geofence  ->  hit?  ->  Telegram
```

* Quiet ticks end with `{"wakeAgent": false}`, so Hermes skips the LLM entirely. No tokens, no spam.
* One shot. A reminder fires once, then it is done. Unfired reminders expire after 7 days.
* Fail silent. If Dawarich is down or the phone stops reporting, nothing is sent. A single warning goes out if the phone has been silent for 24 hours.
* Optional NAS mode moves the polling to an always on Docker box next to Dawarich, which sends through the Telegram Bot API itself. Reminders then fire even when the machine running Hermes is asleep.

### When does it fire?

Either of these:

1. A GPS point inside the radius that is under 30 minutes old and newer than the reminder.
2. The phone's last known position is inside the radius and at most 3 hours old. Phone trackers usually stop logging when you park, so the last point is where you are. If you set the reminder after you got there, it still fires, as long as you arrived no more than 90 minutes before and have been there at least 20 minutes.

## Requirements

* Hermes Agent with a messaging gateway (built and used with Telegram)
* A Dawarich server and its API key
* A phone app feeding Dawarich live points: the official Dawarich apps, OwnTracks, Overland, GPSLogger, PhoneTrack and others work ([Dawarich docs](https://dawarich.app/docs/getting-started/track-your-location))
* macOS or Linux, Python 3.8 or newer. Standard library only, nothing to pip install.
* NAS mode only: a Docker host that can reach Dawarich, `ssh` to it with a key, and sudo there

## Install

From the Hermes skills hub (it runs the normal security scan first):

```
hermes skills install GhostInTheBus/geofence-reminders/skills/location-based-reminders
```

Or by hand:

```
git clone https://github.com/GhostInTheBus/geofence-reminders.git
mkdir -p ~/.hermes/skills/productivity
cp -R geofence-reminders/skills/location-based-reminders ~/.hermes/skills/productivity/
```

## Setup

Run this once in a terminal on the machine running Hermes:

```
bash "$(find ~/.hermes/skills -path '*location-based-reminders/scripts/geofence_setup.sh' | head -1)"
```

It asks for:

1. Your Dawarich URL (as this machine reaches it, for example `http://192.168.1.20:3000`)
2. Your Dawarich API key (hidden prompt, saved to `~/.hermes/secrets/dawarich.env` with chmod 600, never printed)
3. Where Hermes should deliver reminders, in the `--deliver` format, for example `telegram:123456789`

Then it copies the poller to `~/.hermes/scripts/`, checks that Dawarich answers, runs a dry run against a geofence in Antarctica (it should not fire), and creates the `geofence-poller` cron job. Every step is logged to `~/.hermes/logs/geofence_setup.log`. It is safe to re-run.

Then text your agent:

* "remind me when I get to the grocery store to grab detergent"
* "next time I'm at the library return the books"
* "here's today's plan" with a list of stops (every stop gets armed at once)
* "what location reminders do I have", "cancel the grocery store one", "send me a test"

## Health checks

```
python3 ~/.hermes/scripts/geofence_poll.py --selftest
```

```
bash ~/.hermes/scripts/geofence_setup.sh diag
```

```
tail ~/.hermes/logs/geofence_poll.log
```

`--selftest` shows whether Dawarich answers and how old the phone's newest point is. In the log, `pts=0 newest_point=none` on every line means the phone is not reporting.

## NAS mode (optional)

```
bash ~/.hermes/scripts/geofence_setup.sh nas
```

Needs an ssh alias for the NAS (default `nas`) that logs in with a key. It asks for the URL this machine will use to reach the NAS service and for the sudo password once. It builds a small bundle (`geofence_nas.py`, `docker-compose.yml`, `install.sh` and a chmod 600 `geofence.env`), copies it to `/volume1/docker/geofence` by default, starts a `python:3.12-alpine` container, and only switches over once the NAS reports `dawarich=ok` and `telegram=yes`. Still armed reminders move over and a test ping goes out.

Roll back with:

```
bash ~/.hermes/scripts/geofence_setup.sh nas-off
```

Settings for NAS mode live in `~/.hermes/geofence.conf`. See `skills/location-based-reminders/templates/` for every config file format.

## Limitations

* Good for "I'm at the store", not for "I just pulled into the driveway". Expect a few minutes of delay.
* In local mode nothing fires while the machine running Hermes is asleep or off. NAS mode fixes that.
* In local mode the message is relayed by the cron agent, so it takes as long as one short reply from your model.
* Some phone apps upload in batches, so a point can reach Dawarich 10 to 20 minutes late. The 30 minute window and the last known position rule cover that.
* Geocoding uses OpenStreetMap Nominatim, which is thin on some shops. A street address always works.

## Privacy

Your location stays between your phone, your Dawarich server and your Hermes machine. The only outside services are Telegram (the message itself) and OpenStreetMap Nominatim, which receives the place you asked for and, with `--near here`, a search box reaching about 45 km out from your last known position. Secrets live in `~/.hermes/secrets/` and in `geofence.env` on the NAS, all chmod 600, and are never logged. The poller log records counts and ages, not coordinates.

## License

MIT
