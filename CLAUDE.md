# CableTV Simulator - Claude Internal Knowledge

## Project Summary

CableTV Simulator recreates 1990s cable TV with 25 channels of deterministically scheduled content. Development on Windows, deployment on Raspberry Pi + CRT.

**Key Insight**: There is NO stored schedule. Everything is calculated on-demand from the master clock. Same time + same content library + same seed = same output, always.

## Architecture

```
config.yaml          → Config (dataclasses)
cabletv.db           → SQLite (WAL mode)
content/originals/   → Raw video files
content/normalized/  → Transcoded 640x480 4:3 MP4s
commercials/         → Commercial video files
guide/               → Generated guide video segments

app/cabletv/
├── schedule/
│   ├── engine.py           → Timeline building, what_is_on(), two-tier selection
│   ├── commercials.py      → Commercial selection, slot breakdown
│   ├── server_manager.py   → Consumed-slot tracking for server mode
│   └── remote_provider.py  → ScheduleEngine subclass for remote mode
├── playback/
│   ├── engine.py        → Channel switching, segment transitions, position advancement
│   └── mpv_control.py   → mpv TCP IPC (port 9876)
├── guide/
│   ├── generator.py     → Background guide segment generation
│   ├── renderer.py      → Prevue-style scrolling grid (Pillow)
│   └── promos.py        → Promo clip selection
├── network/
│   ├── discovery.py     → mDNS server advertisement + client discovery
│   ├── client.py        → Server connection + API client
│   ├── segment_provider.py → Read guide/weather segments from network share
│   └── smb_instructions.py → First-run share setup instructions
├── ingest/              → 5-stage pipeline
├── interface/           → Flask web API + remote UI + server API
└── utils/               → ffmpeg, time calculations
```

## Core Algorithm

### Two-Tier Content Selection

Content is selected in two steps so that every series/movie gets equal scheduling weight regardless of episode count:

1. **Group**: Pool is grouped by `series_name` (shows) or as standalone items (movies, content without series_name)
2. **Select group**: RNG picks a group uniformly — "Married with Children" (260 eps) gets the same chance as "The Matrix" (1 movie)
3. **Select episode**: Returns the episode at the group's current position for this channel

Groups are cached per channel in `_channel_groups`. Standalone groups contain a single item.

### Sequential Episode Ordering

Episodes play in season/episode order per channel, not randomly:

- **Position tracking**: `series_positions` table stores `(channel_number, group_key) → position`
- **Initial position**: Deterministic from `md5(channel:group_key)` — NOT the session seed, so it's stable across launches
- **Advancement**: Position increments only when the playback engine confirms content actually played (in `tune_to()`)
- **Wrap-around**: `position % len(items)` — handles episodes added/deleted gracefully
- **In-memory cache**: Positions lazy-loaded from DB on first access, persisted on advance

### Schedule Calculation (on every query)

```python
what_is_on(channel=5, when=now)
  1. Calculate slot number from epoch + current time
  2. Walk forward from anchor point to find content block start
  3. Two-tier selection: pick group, then episode at current position
  4. Fetch break points from database
  5. Build timeline: [content, commercial, content, commercial, ..., end padding]
  6. Find which segment we're in based on elapsed time
  7. Return: file path + seek position (content or commercial)
```

### Timeline Building

For a 22-min show with breaks at 7:00 and 14:00 in a 30-min slot:
```
0:00-7:00   = Show (seek 0:00-7:00)
7:00-9:40   = Commercial break #0 (2:40)
9:40-16:40  = Show (seek 7:00-14:00)
16:40-19:20 = Commercial break #1 (2:40)
19:20-27:20 = Show (seek 14:00-22:00)
27:20-30:00 = Commercial break #2 / end padding (2:40)
```

Commercial time is distributed evenly across all breaks (including end).

### Info Bumpers

Every commercial break includes a guaranteed info bumper (5-8 seconds, carved from commercial time):
- Black screen with OSD mini-guide showing current program and next 2 upcoming programs with start times
- Uses `schedule.get_upcoming()` to fetch upcoming program info

### Commercial Selection

Deterministic RNG seeded by: `seed + (channel * 10000) + slot_number + break_index`

- Same break always gets same commercials in same order
- Smart filling: selects commercials that FIT without overshooting
- Standby placeholder if no commercial fits remaining time

**Performance optimizations for large pools (1000+ commercials):**
- Pre-sorted duration list cached at startup
- Binary search O(log n) to find fitting commercials
- Index-based selection (no array shuffling per break)
- Cache rebuilds automatically after ingest pipeline completes

### Seek Precision

- Calculation: millisecond precision
- Actual: ±1 second (limited by keyframes, GOP=30 frames)
- Good enough for authentic cable TV feel

## Key Design Decisions

1. **No stored schedule** - calculated from clock, fully deterministic
2. **TCP IPC for mpv** (port 9876) - cross-platform
3. **30-minute slot grid** - content rounds up to fill slots
4. **Break points from analyzer** - black frame detection finds natural breaks
5. **Relative paths only** - database stores paths relative to drive root
6. **WAL mode SQLite** - concurrent read/write
7. **Grid aligns with real time** - epoch at midnight means slots start at :00/:30
8. **Two-tier selection** - equal weight per series/title, not per episode
9. **Sequential episodes** - position persisted in DB, advances only on actual playback

## Database Schema

```sql
content: id, title, content_type, series_name, season, episode, year,
         duration_seconds, original_path, normalized_path, file_hash,
         tmdb_id, artist, status, ...
tags: id, name, description
content_tags: content_id, tag_id
break_points: id, content_id, timestamp_seconds, confidence
series_positions: channel_number, group_key, position, updated_at
```

Status workflow: scanned → identified → transcoded → ready

`series_positions` tracks where each channel is in a series' episode list. `group_key` is the `series_name` for shows, or `"standalone_{content_id}"` for movies.

## Running

```bash
cd L:\CableTV_Sim\app
python -m cabletv start              # Full system (standalone, fullscreen)
python -m cabletv start --windowed   # Standalone, windowed
python -m cabletv start --server     # Server mode (headless — no video, just API + generators)
python -m cabletv start --server -w  # Server + TV window
python -m cabletv start --remote -w  # Remote client (discovers server, opens video window)
python -m cabletv stats
python -m cabletv schedule now
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze
```

## Content Ingest Workflow

### Adding New Content

When the user adds new video files and wants them ingested, follow this workflow step by step. **The user must see and confirm the output at each step.** Always paste the full CLI output in your response — never summarize or skip it.

#### Step 1: Scan

```bash
cd L:\CableTV_Sim\app
python -m cabletv ingest scan
```

Show the user the count of files added/skipped/errors. Confirm before proceeding.

#### Step 2: AI Identify

```bash
python -m cabletv ingest identify
```

This uses Claude AI + TMDB to identify all `scanned` content automatically. It prints a review table at the end:

```
==========================================================================================
IDENTIFICATION REVIEW
==========================================================================================
   ID  Status  Type    Title                                     Tags
------------------------------------------------------------------------------------------
   12  OK      show    Breaking Bad S01E01                       drama, crime, thriller
   45  OK      movie   The Matrix (1999)                         action, scifi, thriller
------------------------------------------------------------------------------------------
```

**Paste the full review table for the user.** Ask them to review it and confirm everything looks correct before continuing. If anything is wrong, fix it before proceeding (see "Fixing Misidentified Content" below).

Use `--no-ai` to fall back to the old regex+TMDB method if needed.

#### Step 3: Transcode

```bash
python -m cabletv ingest transcode
```

Or `--skip` to use originals as-is. Show the user progress and results.

#### Step 4: Analyze

```bash
python -m cabletv ingest analyze
```

Or `--skip` to skip break point detection. Show the user progress and results.

#### Step 5: Validate

Final validation happens automatically as part of the pipeline. Or run the full pipeline at once:

```bash
python -m cabletv ingest all          # AI identify (default)
python -m cabletv ingest all --no-ai  # regex+TMDB identify
```

### Fixing Misidentified Content

#### Edit metadata directly

```bash
python -m cabletv content edit <id> --title "Correct Title" --tags "drama,comedy"
python -m cabletv content edit <id> --type show --series "Show Name" --season 2 --episode 5
python -m cabletv content edit <id> --year 1995
```

All flags are optional — only the ones specified get changed. `--tags` replaces ALL tags at once (comma-separated).

#### Re-identify from scratch

```bash
python -m cabletv content reset 12 13 45       # reset specific IDs back to "scanned"
python -m cabletv ingest identify               # re-runs AI on those items
```

Reset clears tags and sets status back to `scanned`. Only `scanned` items get processed by identify, so already-done content is never touched.

#### Delete content entirely

```bash
python -m cabletv content delete 12 45 67         # delete specific IDs
```

Deletes the database record AND the files from disk (both original and normalized). Tags, break points, and ingest log entries are cleaned up automatically via cascade.

### Deleting Content (Workflow for Claude)

When the user says "delete X" or "remove those commercials" or "get rid of that":

1. **Find the content** — use `content search` or `content list --type commercial` to find matching items. Show the user the results.
2. **Confirm with the user** — show the IDs and titles that will be deleted. Wait for confirmation.
3. **Delete** — run `python -m cabletv content delete <id1> <id2> ...`
4. **Show output** — paste the full deletion output so the user sees what was removed.

The user may identify content by:
- Filename: search for it with `content search "filename"`
- Title: search with `content search "title"`
- Type: list with `content list --type commercial`
- ID: if they already know the ID, use it directly

### Checking Content

```bash
python -m cabletv content search "matrix"       # search by title, series name, or filename
python -m cabletv content search "matrix" -v    # verbose: also shows tags and series info
python -m cabletv content list                  # all ready content
python -m cabletv content list --status scanned # by status
python -m cabletv content list --type movie     # by type
python -m cabletv content show <id>             # full details for one item
python -m cabletv stats                         # tag counts, totals
```

When the user asks "do we have X?" — use `content search` and show them the output. It searches title, series name, and original filename (case-insensitive).

### Important Notes

- Status is tracked in the SQLite database, not on files. Deleting originals after ingest is fine.
- `original_path` is stored as a string — the AI identifier reads it for filename context, never opens the file.
- Content goes through: scanned → identified → transcoded → ready
- Only `scanned` content is picked up by `identify`. Already-identified/ready content is skipped.
- Valid tags: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, music, mystery, romance, scifi, thriller, western, classic, disney, sitcom, sports
- The AI adds "classic" as a bonus tag for pre-1980s content, and "disney" as a bonus tag for Disney/Pixar content. These stack on top of the normal 2 genre tags.
- Tags must match channel config tags or content won't appear on any channel.

### Post-Ingest Verification

After content finishes the pipeline (all stages complete), always run these checks and show the user the output:

1. **`python -m cabletv stats`** — verify tag distribution makes sense. If a tag has 0 content but a channel uses it, flag it to the user.
2. **`python -m cabletv schedule check-collisions`** — check the same content isn't on two channels at once. Show results.

### Tag-Channel Alignment

Content ONLY appears on a channel if it has at least one tag matching that channel's tags AND a matching content_type. After identification, check that no content is "orphaned" (has tags that don't match any channel).

Current channel tag coverage:
- action: Ch5, 9, 17, 32, 44, 50, 52
- adventure: Ch5, 17
- animation: Ch20, 28
- disney: Ch18
- comedy: Ch3, 9, 15, 22, 34, 50
- crime: Ch16, 17
- classic: Ch32, 34, 55
- documentary: Ch7, 27
- drama: Ch3, 9, 16, 32, 44, 50
- educational: Ch7, 27
- family: Ch3, 9, 22, 28
- fantasy: Ch24
- gameshow: Ch38
- history: Ch27
- horror: Ch40, 52
- kids: Ch20, 28
- music: Ch25
- mystery: Ch16
- romance: Ch36
- scifi: Ch9, 24, 52
- sitcom: Ch15, 34
- sports: Ch38
- thriller: Ch40, 50, 52
- western: Ch42

Movies-only channels: Ch9 (TV 9 Movies), Ch32 (AMC), Ch50 (HBO), Ch52 (Cinemax), Ch55 (TCM). Don't let shows end up with tags that only match these channels unless they're actually movies.

### Channel Configuration

There is no CLI for channel config. To add/change/remove channels, edit `config.yaml` directly. Always show the user the change before saving. Key fields:
- `number`: channel number (what the user tunes to)
- `name`: display name
- `tags`: list of tags — content with ANY matching tag can appear
- `content_types`: list of "movie", "show" — filters what type appears
- `commercial_ratio`: 0.0 = no commercials, 0.2 = 20% commercial time

### Starting the System

`python -m cabletv start` is a **long-running blocking command** (starts mpv + web server). Run it in the background if needed, or warn the user it will block the terminal. Use `--windowed` for development/testing.

`--server` runs headless (no mpv window) — web API + guide/weather generators only. Add `--windowed` to also open a video window. `--remote` requires `--windowed` to display video. See "Network Mode" section below for full details.

## IMPORTANT: Showing Output to the User

**This applies to ALL cabletv CLI commands, not just ingest.**

When running any `python -m cabletv` command:
1. Always run directly via Bash (never in background agents or subagents)
2. Always paste the full output in your text response to the user
3. For ingest steps, wait for user confirmation before proceeding to the next step
4. If output is long (e.g. content list with hundreds of items), show the full output — let the user decide what to focus on
5. When editing content (edit/tag/reset), show the confirmation output so the user sees what changed

## Music Videos (Ch25 — MTV)

Music videos work differently from regular content at every stage.

### How It's Different

| Aspect | Regular Content | Music Videos |
|---|---|---|
| **Detection** | Path checks for "commercial", "bumper", show patterns | Path contains "music video", "music_video", or "music collection" |
| **Content type** | `movie`, `show`, `commercial`, `bumper` | `music` |
| **AI identify** | TMDB tool use, extracts series/season/episode | No TMDB, extracts artist/title/year from filename |
| **DB field** | `series_name`, `season`, `episode` | `artist` (new column) |
| **Tags** | 2-3 genre tags (+ "classic" bonus for pre-1980s) | Always and only `"music"` |
| **Schedule** | Slot-based (30min grid), two-tier selection | Continuous playlist — loops shuffled pool, multiple videos per slot |
| **Commercials** | Fill remaining slot time | None (`commercial_ratio: 0.0`) |
| **OSD** | Channel number + name for 2s | Artist / title / year for 5s at start AND 5s before end |
| **Break points** | Black frame detection | Not needed (skip analyze) |

### Ingest Workflow for Music Videos

```bash
python -m cabletv ingest scan                    # Detects as type "music" from path
python -m cabletv ingest identify                # AI extracts artist/title/year (no TMDB)
python -m cabletv ingest transcode               # Same as regular (or --skip)
python -m cabletv ingest analyze --skip           # No break points needed
```

### Continuous Schedule Mode (`_what_is_on_continuous`)

Channels with `commercial_ratio: 0.0` bypass the slot-based engine entirely:
1. Pool sorted by content ID, then shuffled with seed `seed + channel_number`
2. Total playlist duration = sum of all item durations
3. Position = `elapsed_from_epoch % total_playlist_duration` (loops forever)
4. Walk playlist to find current item and seek position
5. Same time = same video at same position (deterministic)

### Config (config.yaml)

```yaml
- number: 25
  name: "MTV"
  tags: ["music"]
  content_types: ["music"]
  commercial_ratio: 0.0
```

### CLI

```bash
python -m cabletv content edit <id> --artist "Artist Name"    # Set artist
python -m cabletv content edit <id> --type music --tags "music"  # Fix misdetected
python -m cabletv content list --type music                    # List music content
```

## Guide Channel (Ch14 — Preview Channel)

Prevue-style scrolling grid channel with promo clips.

### How It Works

- `guide/generator.py` runs in background, rendering video segments
- `guide/renderer.py` draws the scrolling grid using Pillow (channel names, times, programs)
- `guide/promos.py` selects promo clips to play in the upper portion
- Segments are pre-rendered to `guide/` directory and played by mpv with loop
- Playback engine polls every 5 seconds for new segments

### Config (config.yaml)

```yaml
guide:
  enabled: true
  channel_number: 14
  promo_duration: 20
  scroll_speed: 3.0
  segment_duration: 600
  regenerate_interval: 600
  fps: 15
```

### CLI

```bash
python -m cabletv guide generate [--short]    # Generate guide segments manually
```

## Weather Channel (Ch26 — The Weather Channel)

90s-style Weather Channel with cycling data pages, scrolling forecast ticker, and optional smooth jazz background music.

### Architecture

```
app/cabletv/weather/
├── __init__.py       → Package docstring
├── api.py            → Open-Meteo + RainViewer fetching, data models, caching
├── renderer.py       → 6-page Pillow renderer + scrolling ticker + brand bar
├── generator.py      → Background segment generation (mirrors guide/generator.py)
├── icons.py          → Programmatic weather icons drawn with Pillow
└── moon.py           → Pure-Python moon phase calculation
```

### How It Works

- `weather/generator.py` runs in background, pre-rendering video segments
- `weather/renderer.py` draws 6 cycling pages using Pillow (current conditions, forecast, almanac, etc.)
- `weather/api.py` fetches data from Open-Meteo API (no API key needed), caches for `refresh_interval`
- Segments are pre-rendered to `weather/` directory and played by mpv with loop
- Playback engine polls every 5 seconds for new segments (same pattern as guide)
- Weather data refreshes every `refresh_interval` seconds (default 1 hour)

### 6 Pages (10 seconds each)

1. **Current Conditions** — Large temp, icon, conditions text, humidity/wind/barometer/dewpoint/visibility/feels-like
2. **Today's Forecast** — Today high/conditions + Tonight low/conditions + Tomorrow preview
3. **Extended Forecast** — 5-day horizontal grid with icons and high/low temps
4. **Almanac** — Sunrise/sunset, day length, moon phase with drawn moon circle
5. **Hourly Forecast** — 12-row table with time, temp, icon, precip% (alternating rows)
6. **Regional Radar** — RainViewer radar with retro green-on-black colormap, OR **Regional Temperatures** fallback

### APIs

- **Open-Meteo** (`api.open-meteo.com/v1/forecast`): Free, no API key. Single call gets current + hourly + daily + sunrise/sunset.
- **RainViewer** (`api.rainviewer.com`): Free radar tiles. 3x3 tile grid at zoom 7, stitched and colormapped.

### Config (config.yaml)

```yaml
- number: 26
  name: "Weather Channel"
  tags: []
  content_types: []
  commercial_ratio: 0.0

weather:
  enabled: true
  channel_number: 26
  latitude: 35.3965
  longitude: -79.0028
  location_name: "Lillington, NC"
  segment_duration: 60
  page_duration: 10
  refresh_interval: 3600
  fps: 15
  radar_enabled: true
  background_music: "L:\\CableTV_Sim\\music\\weather.mp3"
```

### CLI

```bash
python -m cabletv weather generate    # Generate a weather segment for testing
```

## Network Mode (Server/Remote)

Allows multiple PCs on the same LAN to view the same broadcast — same channel, same time, same content, same seek position. One PC (server) has the content drive; others (remotes) access it over a network share.

### Architecture

```
SERVER (--server):
  Headless by default (no mpv window). Runs:
  - Web server + API endpoints + mDNS advertisement
  - Guide/weather segment generation
  - ServerScheduleManager wraps ScheduleEngine with consumed-slot tracking
  With --windowed: also opens mpv and acts as a TV (advances routed through ServerScheduleManager)

REMOTE (--remote):
  - Discovers server via mDNS (or manual server_url in config)
  - Fetches server's seed at startup
  - Opens cabletv.db from network share READ-ONLY
  - Runs LOCAL ScheduleEngine with server's seed (zero-latency channel switching)
  - Position advances go to server API (fire-and-forget with local fallback)
  - Content files resolved via network share (content_root)
  - Guide/weather segments read from network share via JSON sidecars (no local generation)
  - Local mpv, local OSD, local web remote
```

### Key Files

```
app/cabletv/network/
├── __init__.py           → Package init
├── discovery.py          → mDNS advertisement (ServerAdvertiser) + discovery (ServerDiscoverer)
├── client.py             → ServerConnection — connect to server, fetch info/positions
├── segment_provider.py   → RemoteSegmentProvider — read guide/weather from share
└── smb_instructions.py   → First-run network share setup instructions

app/cabletv/schedule/
├── server_manager.py     → ServerScheduleManager — consumed-slot tracking
└── remote_provider.py    → RemoteScheduleProvider — ScheduleEngine with server's seed

app/cabletv/interface/
└── server_api.py         → Flask Blueprint: /api/server/info, /advance, /positions
```

### Config (config.yaml)

```yaml
network:
  mode: standalone         # "standalone" | "server" | "remote"
  server_url: ""           # Manual fallback: "http://192.168.1.100:5000"
  content_root: ""         # Network share path for remote: "\\\\SERVER\\CableTV_Sim"
  server_name: "CableTV Server"  # mDNS service name
  discovery_timeout: 10    # Seconds to wait for mDNS discovery
```

### CLI

```bash
# Server — headless (no video window, just API + generators):
python -m cabletv start --server

# Server + TV — also opens a video window:
python -m cabletv start --server --windowed

# Remote (another PC on the LAN):
python -m cabletv start --remote --windowed
```

CLI flags override `mode` in config.yaml.

### Server Setup

1. Run `python -m cabletv start --server` — headless, prints seed + SMB setup instructions on first run
2. Share the CableTV_Sim folder as a network share (read-only for remotes)
3. Server advertises via mDNS — remotes auto-discover it
4. Optionally add `--windowed` if the server PC should also display video

### Remote Setup

1. Map/mount the server's network share
2. Set `content_root` in config.yaml to the share path (e.g. `\\SERVER\CableTV_Sim`)
3. Optionally set `server_url` if mDNS doesn't work (e.g. `http://192.168.1.100:5000`)
4. Run `python -m cabletv start --remote --windowed`

### Web Remote Control

Each instance (server or remote) runs its own web server on port 5000. Connect to that machine's IP to control its TV:
- Server at `10.2.0.2` → `http://10.2.0.2:5000` controls the server's TV (if `--windowed`)
- Remote at `10.2.0.50` → `http://10.2.0.50:5000` controls that remote's TV

The web remote only controls the local mpv. The only cross-network traffic is position advances.

### How Broadcast Consistency Works

- **Same content**: Deterministic from shared seed (remote uses server's seed)
- **Same seek position**: Calculated from clock — `(now - block_start_time)`, NTP keeps clients in sync within ~1s
- **Same episode**: Positions loaded from server, shared across all clients
- **Position advances once**: `ServerScheduleManager` tracks consumed `(channel, block_start_slot)` pairs; first client to advance wins. Server's own playback (when `--windowed`) also routes through this, preventing double-advances.

### Dependencies

- **Server**: `pip install zeroconf` (for mDNS advertisement)
- **Remote**: `pip install requests` (for server API calls). `zeroconf` is optional — needed for auto-discovery, not required if `server_url` is set manually in config.

### Troubleshooting

- **Remote can't find server**: Check both PCs are on same LAN subnet. Try setting `server_url` manually in config.yaml.
- **Content won't play on remote**: Verify `content_root` path exists and contains `cabletv.db` and `content/` directory.
- **Schedule mismatch**: Server restarts generate a new random seed. Restart remote clients after server restarts.
- **Guide/weather not showing on remote**: Ensure `guide/` and `weather/` directories exist on the share. Server must be generating segments (JSON sidecars are written alongside each segment file).

## Implementation Status

### Complete
- Deterministic schedule engine with timeline-based commercial breaks
- Two-tier content selection (equal weight per series/title, not per episode)
- Sequential episode ordering with DB-persisted positions
- 5-stage ingest pipeline (scan, identify, transcode, analyze, register)
- AI-powered content identification (Claude API + TMDB tool use)
- Smart commercial selection (fits duration, handles gaps)
- Info bumpers (mini-guide OSD in every commercial break, 5-8s)
- mpv playback with segment transitions (content, commercial, info bumper)
- Web remote control
- 27 channels configured (25 content + 1 guide + 1 weather; real cable names: MTV, Comedy Central, Sci-Fi Channel, HBO, Disney Channel, etc.)
- Content edit/reset CLI commands for fixing misidentifications
- Music video channel (Ch25 — MTV) with continuous playlist, artist OSD, no commercials
- Guide channel (Ch14 — Preview Channel) with Prevue-style scrolling grid + promo clips
- Weather channel (Ch26 — The Weather Channel) with cycling data pages, scrolling ticker, radar
- "Classic" bonus tag auto-assigned by AI for pre-1980s content
- "Disney" bonus tag auto-assigned by AI for Disney/Pixar content
- Network mode: server (headless or with window) / remote operation with mDNS discovery, shared seed, consumed-slot tracking, JSON segment sidecars

### Not Implemented
- GPIO/IR input on Pi
- Standby video (currently OSD text only)

## Known Limitations

1. **Callbacks fired inside lock** - Don't call `tune_to()` from callbacks (deadlock risk)

## CRITICAL Rules for Database Changes

**NEVER modify the database schema or run migrations without telling the user first.** The database contains hours of AI-identified metadata and tag associations that are expensive to recreate.

Before ANY schema change:
1. **Tell the user** what you're about to change and why
2. **Back up the database** (`cabletv.db` → `cabletv.db.bak`) before touching anything
3. **Never use ALTER TABLE RENAME** with `foreign_keys=ON` — it corrupts FK references in dependent tables
4. **Never use `skip_transcode`** on content that was previously transcoded — it overwrites `normalized_path` with `original_path`, breaking playback if originals were deleted
5. **Test migrations on a copy first**, not the live database
6. The `_run_migrations()` function in `db.py` auto-backs up before changes, but Claude should STILL tell the user before adding new migrations

## Common Issues

1. **Content not showing**: Check tags match channel config
2. **mpv won't start**: Verify mpv in PATH
3. **Schedule seems wrong**: Remember it's deterministic - same time = same content
4. **Info bumper not showing**: Only displays when gap is >= 3 seconds
