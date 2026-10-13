# warden-icarus

Discord bot that administers the **Icarus** dedicated server (Docker image [`mornedhels/icarus-server`](https://github.com/mornedhels/icarus-server)) on the Debian 13 VM.
It replaces the bot built into the old WindowsGSM fork and keeps its persistent panels (Components V2), adapted to Docker.

The bot's Discord interface (panels, buttons, messages) is in French; labels are quoted below as they appear in Discord.

| Channel | Content |
|---|---|
| **Panel** | Container state, server name, players (A2S), CPU (% of the whole VM, comparable to Proxmox, e.g. "CPU 4 % (4 cœurs)") and Icarus container RAM without cache (e.g. "RAM Icarus 1,2 Go / 7,7 Go"), uptime, refreshed every 60 s. Buttons Démarrer / Arrêter / Redémarrer / Mettre à jour / Infos (start / stop / restart / update / info). |
| **Backups** | List of saves (prospects), manual backup (timestamped `.tar.gz` archive, with retention), restore with confirmation, download of a map from Discord. |
| **Settings** | Alert toggles, scheduled restart (disabled by default), editable Icarus settings: `.env` variables (name, masked passwords, max players…, applied by recreating the container) and `ServerSettings.ini` keys (saving, respawn…, applied by a restart). |
| **Logs** | Who did what, important container lines (anti-spam), player arrivals and departures, alerts mentioning the admin role. |

Slash commands: `/aide`, `/statut`, `/stats` (everyone); `/demarrer`, `/arreter`, `/redemarrer`, `/maj`, `/sauvegarde`, `/logs`, `/panneaux` (admin role).
Info, `/statut` and `/stats` are read-only and open to everyone. Every other action requires the admin role and is logged in the logs channel.

---

## 1. Installation on the VM

All commands run **on the VM** (`ssh lucky@192.168.1.109`), as `lucky`.

### 1.1 Prerequisites

```bash
docker --version && docker compose version   # Docker and the compose plugin are already there for Icarus
id -nG | grep -qw docker && echo "OK: lucky is in the docker group"
```

### 1.2 Create the Discord bot

1. <https://discord.com/developers/applications> → **New Application** (for example "Warden Icarus").
2. **Bot** tab → **Reset Token** → copy the token (used for `DISCORD_TOKEN`). No *Privileged Gateway Intent* is needed; leave them all disabled.
3. **OAuth2 → URL Generator** tab: scopes `bot` and `applications.commands`. Permissions: *View Channels*, *Send Messages*, *Read Message History*, *Mention Everyone* (to mention the admin role in alerts). Open the generated URL and invite the bot to your server.
   The invite link is also written to the bot logs at every start.
4. In Discord, enable **Developer Mode** (Settings → Advanced), then right-click to copy:
   - the server ID (`GUILD_ID`);
   - the admin role ID (Server Settings → Roles → right-click → `ADMIN_ROLE_ID`).
5. Check that the bot can **see the 4 channels, write in them and read their history**.

> If the old WindowsGSM bot used **the same bot account**, stop it first. On first start, warden-icarus deletes its own older messages in each panel channel, which removes the old bot's panels. If it was **another account**, delete the old panels by hand.

### 1.3 Adapt the Icarus compose file (once)

The bot rewrites `/home/lucky/icarus/.env` and `data/Saved/Config/WindowsServer/ServerSettings.ini`, never the compose file. The `docker-compose.yml` must therefore read each setting from the `.env` (`${VAR}`) and translate it to the name the image expects:

| `.env` (names managed by the bot) | `mornedhels/icarus-server` variable |
|---|---|
| `SERVERNAME` | `SERVER_NAME` |
| `JOIN_PASSWORD` / `ADMIN_PASSWORD` | `SERVER_PASSWORD` / `SERVER_ADMIN_PASSWORD` |
| `MAX_PLAYERS` | `SERVER_MAX_PLAYERS` |
| `SHUTDOWN_NOT_JOINED_FOR` / `SHUTDOWN_EMPTY_FOR` | `SERVER_SHUTDOWN_IF_NOT_JOINED` / `SERVER_SHUTDOWN_IF_EMPTY` |
| `ALLOW_NON_ADMINS_LAUNCH` / `ALLOW_NON_ADMINS_DELETE` | `SERVER_ALLOW_NON_ADMINS_LAUNCH` / `SERVER_ALLOW_NON_ADMINS_DELETE` |
| `RESUME_PROSPECT` | `SERVER_RESUME_PROSPECT` |

Volumes: `./data` → `/home/icarus/drive_c/icarus`, `./game` → `/opt/icarus`. `stop_grace_period: 90s` gives the game time to save (like supervisord's `stopwaitsecs` in the image). `UPDATE_CRON` makes the image check for updates, which it only installs when the server is empty.

`SaveGameOnExit`, `GameSaveFrequency`, `FiberFoliageRespawn` and `LargeStonesRespawn` are **no longer environment variables**: the image does not handle them. The bot changes them directly in `ServerSettings.ini` (section `[/Script/Icarus.DedicatedServerSettings]`). The old `SAVEGAMEONEXIT=…`, `GAMESAVEFREQUENCY=…`, `FIBERFOLIAGERESPAWN=…` and `LARGESTONERESPAWN=…` lines of the `.env` are no longer used and can be removed. After the first start with the new image, check their values in the settings channel (📄) and fix them if needed.

First get the bot code:

```bash
cd ~
git clone https://github.com/Lucky12348/warden-icarus.git   # adapt the URL to your repository
```

Then migrate the Icarus compose file. The originals are kept as `.orig`:

```bash
cd /home/lucky/icarus
cp docker-compose.yml docker-compose.yml.orig
cp .env .env.orig
cat ~/warden-icarus/deploy/icarus/env.additions >> .env
cp ~/warden-icarus/deploy/icarus/docker-compose.yml docker-compose.yml
chmod 600 .env
mkdir -p backups
docker compose config --quiet && echo "compose OK"
docker compose up -d        # recreates the container with the same configuration
```

Check that the variables are taken into account (passwords are not shown):

```bash
docker inspect icarus --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(SERVER_NAME|SERVER_MAX_PLAYERS|SERVER_SHUTDOWN_IF_EMPTY|UPDATE_CRON)='
grep -E '^(SaveGameOnExit|GameSaveFrequency|FiberFoliageRespawn|LargeStonesRespawn)=' data/Saved/Config/WindowsServer/ServerSettings.ini
```

The bot writes `ServerSettings.ini` as `lucky` (uid 1000): the compose file sets `PUID=1000` and `PGID=1000` so the image creates its files with that owner. No extra mount is needed; the whole Icarus folder is already mounted in the bot container.

### 1.4 Install the bot

```bash
cd ~/warden-icarus
cp .env.example .env
chmod 600 .env
getent group docker | cut -d: -f3        # → put this in DOCKER_GID
nano .env                                # DISCORD_TOKEN, GUILD_ID, ADMIN_ROLE_ID, DOCKER_GID (+ ntfy if wanted)
mkdir -p state                           # must belong to lucky (uid 1000), not root
docker compose up -d --build
docker compose logs -f                   # Ctrl+C to stop following
```

The logs should show `Connecté en tant que …` then `… slash commands synchronisées`. In Discord, the three panels appear and the logs channel receives "warden-icarus démarré".

### 1.5 ntfy (optional)

In `~/warden-icarus/.env`:

```dotenv
NTFY_URL=https://ntfy.sh          # or your own instance
NTFY_TOKEN=tk_xxxxxxxx            # if the topic is protected
NTFY_TOPIC=icarus
```

Apply with `docker compose up -d`, then enable the **ntfy** toggle in the settings channel.

---

## 2. How it works

### Server panel
- The image updates Icarus **by itself**: at every container start, then according to `UPDATE_CRON`, and only when the server is empty.
- **Mettre à jour** (update) forces an update: the bot runs `supervisorctl start icarus-force-update` in the container. The image deletes the installed version, downloads the game again, then restarts it; the server is unavailable during the download. Refused if players are connected (the image would not update) or if the server is stopped (use **Démarrer**, which already checks for updates).
- **Redémarrer** (restart) restarts the container (`docker restart`).
- The bot compares the Steam *build* (`game/server/steamapps/appmanifest_2089300.acf`, otherwise `game/current_version`) and posts "Mise à jour installée" when it changes.
- Stops give the game **90 s** by default (`STOP_TIMEOUT`, 60 s minimum) to save.
- If players are connected, Arrêter / Redémarrer ask for confirmation.
- One operation at a time: the other buttons wait until it finishes.

### Players
A2S queries `127.0.0.1:32784` every 20 s. If Icarus does not return names, the panel shows "2/8 joueurs" and the logs channel announces "Un joueur a rejoint la partie".
Arrivals are announced at once; a departure only once the player has been missing for `PLAYER_LEAVE_GRACE_SECONDS` (120 s by default). Joining briefly disconnects the player (lobby, then prospect loading), which would otherwise show "a rejoint / a quitté / a rejoint" within a minute; a reconnection within the grace period publishes nothing. `0` restores immediate departures.
Log-based detection can be enabled later (`PLAYER_DETECTION=logs` or `both`, with `join`/`leave` patterns in a patterns file, see below).

### Backups
- **Archives**: `/home/lucky/icarus/backups/icarus-YYYYMMDD-HHMMSS.tar.gz`. Each archive contains all of `data/Saved/PlayerData` (saves and player data), without the game's `.backup_N` files, alongside a `.manifest.json` file. Only the `BACKUP_RETENTION` newest are kept. A backup can run with the server on: each `.json` is re-read until it is valid JSON.
- **Download** (**Télécharger** button, admin role): sends the current map save (`<save>.json`, zipped) privately in Discord, visible only to the admin who clicked. With a single map, one click is enough; otherwise a menu lists the maps. It works with the server running (the file is re-read until it is valid JSON). If the zip is larger than the server's upload limit (10 MB without boost), it is split into `.zip.001`, `.zip.002`… parts, with instructions to join them (7-Zip, or `copy /b` on Windows); above 10 parts, the bot gives the file's path on the VM instead. Each download is logged in the logs channel.
- **Restore**: choose the save, then the backup (game `.backup_N`, `.pre_restore_*` or bot archive), then confirm. The bot stops the server, copies the current save to `<save>.json.pre_restore_<date>`, replaces it with the chosen backup (which is kept), then restarts the server. A backup with invalid JSON is refused and nothing is changed.

### Settings
- Settings are picked from the menu, then entered in a modal and validated (bounds, True/False).
- **`.env` settings** (name, passwords, max players, automatic shutdowns, non-admin rights, prospect resume): the bot shows a confirmation with three choices, **Appliquer et recréer** (apply and recreate), **Enregistrer seulement** (save only) or **Annuler** (cancel). The Icarus `.env` is rewritten cleanly: comments and order kept, `.env.bak` copy, atomic write, then a `docker compose config` check. If anything fails, the `.env.bak` made by that write is restored. Writes happen under the operation lock, so two admins cannot overwrite each other.
- **`ServerSettings.ini` settings** 📄 (save on exit, save frequency, fibre and large rock respawn): after confirmation (**Appliquer et redémarrer**, apply and restart), the bot stops the server cleanly (the game rewrites this file, so a change made while it runs could be lost), copies the file to `ServerSettings.ini.bak`, changes the key (the rest of the file, its encoding and its line endings are kept), then restarts the server. With the server stopped, the file is changed and the setting applies at the next start.
- Passwords are **never shown**: "•••••• (défini)". In the modal, an empty field leaves the password unchanged and a lone `-` removes it. The image ignores an empty variable, so for a removal the bot also clears the key in `ServerSettings.ini` during the recreation (only **Appliquer et recréer** is offered).
- ⏳ marks a setting changed but not applied yet (the `.env` differs from the running container). 🔒 marks a setting hard-coded in the compose file.

### Alerts
| Event | How it is detected |
|---|---|
| 💥 Crash | Docker `die` event with an exit code ≠ 0 (or `oom`), with no bot action in progress |
| 🔁 Unexpected restart | Restart by Docker after a crash, or `docker restart` run outside Discord |
| ⬆️ Update | The installed Steam build changed |

Alerts mention the admin role in the logs channel and are also sent to ntfy if the toggle is on. They go through an anti-spam (at most 5 alerts per 10 min, same type at most every 2 min).
With `mornedhels/icarus-server`, the game runs under supervisord **inside** the container: if the game crashes, supervisord restarts it without the container stopping, so with no Docker event. These crashes show up in the logs channel as 💥 lines when the game writes a recognised message (see "Container logs"), not as a "Crash" alert. The "Crash" alert is still raised if the container itself exits with an error. An update done by the image (cron or button) does not restart the container and raises no restart alert.
If the container exits with a code ≠ 0 during a normal stop, add that code to `CLEAN_EXIT_CODES` (for example `0,1`).

### Scheduled restart
**Disabled by default**: the image already handles updates (`UPDATE_CRON`, empty server), so the daily restart is no longer needed. The settings channel says so. On the first start of this version, a scheduled restart enabled in `state/state.json` is disabled once; you can enable it again afterwards, and that choice is kept.
Once enabled (06:00 Europe/Paris by default, the time can be changed in the settings channel), the bot announces it in the logs channel 5 min before. If players are connected, or if A2S does not answer, it does not restart and retries every 15 min for 2 h at most, then gives up for the day. A server already stopped is not started.

### Container logs
**Normalisation.** The image sends the output of each supervisord program (`icarus-server`, `icarus-updater`, `icarus-force-update`, `crond`…) through rsyslog, which adds a header and escapes the final carriage return as `#015`:

```
2026-10-12T18:38:27.416174+00:00 icarus supervisord: icarus-server <message>#015
```

Before any processing, the bot removes the timestamp, `icarus supervisord: <program> ` and the `#015`/`\r`; tabs escaped as `#011` become tabs again. The program name is kept separately and shown as `[icarus-server] <message>`. All patterns apply to the message alone with `re.search`: `^` and `$` mean the start and end of the message.

**Whitelist.** Only these categories are published in the logs channel, in addition to admin actions and alerts:

| Category | Lines recognised by default |
|---|---|
| ✅ start-up | `Engine is initialized` |
| ⬆️ update | `Success! App '2089300' fully installed` / `already up to date` (icarus-updater), **only if the installed build just changed**: a check without an update, or a reinstall of the same build, is not published |
| 💥 crash | `Fatal error`, `Ran out of memory`, `Freeing … backup pool`, `Assertion failed`, `Unhandled Exception`, `exited: icarus-server (… not expected)`, `gave up: icarus-server entered FATAL state` (an unexpected stop of the container itself is reported by the "Crash" alert) |
| 👋 / 🚪 arrival / departure | no default pattern (detection through A2S, see "Players") |

The `error` category (any line containing `error` or `failed`) is **no longer published**: Unreal under Wine writes such lines constantly with no consequence. `ERROR_LOGS_TO_DISCORD=true` in the bot's `.env` enables it again. Known noise is dropped by the `ignore` category anyway, even with that option: Unreal warnings (`LogTemp: Warning: …`, `LogTalentGame: Warning: …`, `LogAnimMontage: Warning: …`, except those announcing a crash), `IcarusOSSLog: Error`, `LogIcarusGameStateRecording: Error`, Wine (`0050:err:vulkan:…`, `fixme:`…), steamcmd, and rsyslog repetitions (`message repeated 6 times: [ … ]`).

**Anti-spam.** Published lines are grouped every 5 s, with two limits: the same line at most every 5 min (`LOG_DEDUPE_SECONDS`) and at most 10 lines per minute (`LOG_MAX_PER_MINUTE`). Dropped lines are no longer announced. With `LOG_SPAM_SUMMARY=true`, a discreet summary ("N ligne(s) non publiée(s)") is sent at most once per hour.

**`/logs [lignes=50]`** (admin role): replies privately (ephemeral) with the last N container lines (2000 at most), normalised but **not filtered**. If they do not fit in one message, they arrive as a `.txt` file.

**Customising the patterns**: copy the example file into the state folder.

```bash
cp ~/warden-icarus/patterns.example.json ~/warden-icarus/state/patterns.json
nano ~/warden-icarus/state/patterns.json       # categories: ignore, crash, join, leave, update, startup, error
echo 'LOG_PATTERNS_FILE=/app/state/patterns.json' >> ~/warden-icarus/.env
cd ~/warden-icarus && docker compose up -d
```

Each category present in the file replaces the default one (if you define `ignore`, keep the example's patterns and add yours; without an `ignore` key, the default patterns apply). The same normalisation applies: write patterns for the message alone, without timestamp or `#015`. For `join`/`leave`, the named group `(?P<name>…)` captures the player name. This file decides a line's category; the whitelist above decides whether it is published.

### Security
- The Docker socket **is equivalent to root on the VM**. The bot only uses it through `warden/docker_ctl.py`, which acts only on the container named `icarus`, and only to read its state, statistics, logs and events, start, stop, restart or recreate it. For a recreation, the bot checks that the compose project is in `/home/lucky/icarus`. A single `exec`, with a fixed command and no parameter: `supervisorctl start icarus-force-update` (**Mettre à jour** button). No other `exec`, no other container. This limit is enforced by the code, not by Docker: protect the Discord token and access to the VM accordingly.
- **The game folder is treated as untrusted.** `data/` is writable by the Icarus server, which is exposed to the Internet. A compromised game could place symlinks there to make the bot read or overwrite another file (the Icarus `docker-compose.yml`, which would give root on the VM at the next recreation, or `/proc/self/environ`, which holds the Discord token). `warden/safefs.py` therefore never follows a symlink: files are read with `O_NOFOLLOW` and must be regular files of bounded size; writes go through a temporary file with an unpredictable name and then an atomic rename, which replaces a planted link instead of writing through it; and on Linux every folder below `data/` is opened one level at a time through directory descriptors without following links, so a folder swapped for a link at the last moment cannot redirect a read or a write. Saves, restores, downloads, backups and `ServerSettings.ini` all go through it.
- **Open commands are throttled.** Info, `/statut` and `/stats` (open to every member) share one `docker stats` result for 10 s, and "Actualiser" only edits the panel when it changed, so repeated clicks cannot saturate the bot.
- **Untrusted text is never interpreted as markdown.** Player names, save names (chosen in game), Discord nicknames and values read from `ServerSettings.ini` are escaped before being shown, so nobody can put a fake link or fake formatting in a message published by the bot. Mentions are disabled except for the admin role in alerts.
- The bot container runs with uid 1000, the host's docker group, a read-only file system, `no-new-privileges` and no capability.
- Secrets (`DISCORD_TOKEN`, `NTFY_TOKEN`, Icarus passwords) stay in `.env` files with `chmod 600`, never in Git nor on Discord.

---

## 3. Updating the bot

```bash
cd ~/warden-icarus
git pull
docker compose up -d --build
docker compose logs --tail=50
```

Back to the previous version: `git log --oneline`, then `git checkout <commit>` and `docker compose up -d --build`.
The bot state (toggles, scheduled time, panel IDs) is kept in `state/state.json`.

---

## 4. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `DOCKER_GID manquant` on `docker compose up` | Set `DOCKER_GID` in `.env` (`getent group docker \| cut -d: -f3`). |
| `Configuration invalide : …` in the logs | A required variable is missing or malformed in `.env`. The message says which one. |
| `Permission denied` on `/var/run/docker.sock` | `DOCKER_GID` does not match the socket's owning group: `stat -c %g /var/run/docker.sock`. |
| `PermissionError` on `state/state.json` | `state/` was created by root: `sudo chown -R 1000:1000 ~/warden-icarus/state`. |
| Panels do not appear | The bot must see the channel, write in it and read its history. Also check the channel IDs. `/panneaux` forces a republish. |
| Slash commands do not appear | Check `GUILD_ID` and that the bot was invited with the `applications.commands` scope. Reload Discord (Ctrl+R). |
| "Joueurs : — (le serveur ne répond pas)" | Normal during start-up or an update (up to 15 min). Otherwise test A2S from the VM: `docker exec warden-icarus python -c "import a2s;print(a2s.info(('127.0.0.1',32784)))"`. |
| Admin role mentioned without a notification | Give the bot the *Mention @everyone…* permission or make the role mentionable. |
| Setting marked 🔒 | The setting is hard-coded in the Icarus `docker-compose.yml`: replace the value with `${VAR}` and add `VAR=…` to the `.env` (see 1.3). |
| Recreation fails | Read the message in Discord. By hand: `cd /home/lucky/icarus && docker compose config --quiet && docker compose up -d`. The previous `.env` is in `.env.bak`. |
| `PermissionError` on `ServerSettings.ini` | The file does not belong to `lucky`: check `PUID=1000`/`PGID=1000` in the Icarus compose file, then `sudo chown 1000:1000 ~/icarus/data/Saved/Config/WindowsServer/ServerSettings.ini`. |
| "ServerSettings.ini introuvable" | The image creates it on first start: start the server once. A copy of the previous version is in `ServerSettings.ini.bak`. |
| "… lien symbolique : refusé" / "… sort de … : refusé" | A file or folder in `data/` was replaced by a symlink. This is not normal: check `ls -la` on the folder named in the message and remove the link. If you did not create it, consider the game server compromised. |
| **Mettre à jour** fails (`supervisorctl a échoué`) | Read the message. By hand: `docker exec icarus supervisorctl status`, then `docker exec icarus supervisorctl start icarus-force-update`. |
| False "Crash" alerts every ~5 min | The container exits with a code ≠ 0 when stopping normally: add that code (shown in the alert) to `CLEAN_EXIT_CODES`. |
| Too many or too few logs | Adjust `LOG_MAX_PER_MINUTE` and `LOG_DEDUPE_SECONDS`, `ERROR_LOGS_TO_DISCORD`, or the patterns (see "Container logs"). To see everything the server writes: `/logs`. |
| Error, or "Interaction failed" on a button | `docker compose logs --tail=100 warden`. Errors are detailed there. |

Useful commands:

```bash
cd ~/warden-icarus
docker compose ps                         # bot state (healthy?)
docker compose logs -f --tail=100         # bot logs
docker compose restart                    # restart the bot
sed -i 's/^LOG_LEVEL=.*/LOG_LEVEL=DEBUG/' .env && docker compose up -d   # more detail (set back to INFO afterwards)
docker logs --tail=100 icarus             # raw Icarus server logs
```

---

## 5. Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                    # backups, settings, anti-spam, scheduling, events, symlink protection…
```

The symlink tests (`tests/test_safefs.py`) are skipped on Windows when symlinks cannot be created; run them on Linux, for example on the VM without installing anything:

```bash
docker run --rm -v ~/warden-icarus:/src -w /src python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && pytest -rs"
```

Layout:

```
warden/
  __main__.py      entry point (python -m warden)
  config.py        configuration (the bot's .env)
  bot.py           Discord client, panels, loops (players, events, logs, scheduling)
  handlers.py      buttons, menus, modals (server, backups, settings)
  commands.py      slash commands
  panels.py        panel content
  ui.py            Components V2 rendering, escaping of untrusted text
  docker_ctl.py    Docker access restricted to the icarus container
  safefs.py        file access in data/ without following symlinks
  backups.py       archives, retention, restore
  settings_store.py reading/writing the Icarus .env and ServerSettings.ini
  events.py        classification of Docker events (crash, unexpected restart…)
  scheduler.py     scheduled restart
  players.py       A2S and arrival/departure detection
  logpatterns.py   log line normalisation and sorting
  antispam.py      anti-spam
  notify.py        logs channel and ntfy
  state.py         persistent state (state/state.json)
deploy/icarus/     Icarus compose file and .env adapted to the bot
tests/             unit tests (pytest)
```
