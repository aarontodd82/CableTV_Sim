# CableTV Simulator

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
weather/             → Generated weather video segments

app/cabletv/
├── schedule/
│   ├── engine.py           → Timeline building, what_is_on(), two-tier selection
│   ├── commercials.py      → Commercial selection, slot breakdown
│   ├── server_manager.py   → Consumed-slot tracking for server mode
│   └── remote_provider.py  → Thin API client for remote mode
├── playback/
│   ├── engine.py        → Channel switching, segment transitions, position advancement
│   └── mpv_control.py   → mpv TCP IPC (port 9876)
├── guide/
│   ├── generator.py     → Background guide segment generation
│   ├── renderer.py      → Prevue-style scrolling grid (Pillow)
│   └── promos.py        → Promo clip selection
├── weather/
│   ├── api.py           → Open-Meteo + RainViewer fetching, data models, caching
│   ├── renderer.py      → 6-page Pillow renderer + scrolling ticker + brand bar
│   ├── generator.py     → Background segment generation
│   ├── icons.py         → Programmatic weather icons drawn with Pillow
│   └── moon.py          → Pure-Python moon phase calculation
├── network/
│   ├── discovery.py     → mDNS server advertisement + client discovery
│   ├── client.py        → Server connection + API client
│   └── segment_provider.py → HttpSegmentProvider (fetches segments via server API)
├── interface/
│   ├── web.py           → Flask web UI + remote control
│   └── server_api.py    → Server API endpoints for remote clients
├── ingest/              → 5-stage pipeline (scan, identify, transcode, analyze, validate)
└── utils/               → ffmpeg, time calculations
```

## Core Algorithm

### Two-Tier Content Selection

Content is selected in two steps so that every series/movie gets equal scheduling weight regardless of episode count:

1. **Group**: Pool is grouped by `series_name` (shows) or as standalone items (movies)
2. **Select group**: RNG picks a group uniformly — "Married with Children" (260 eps) gets the same chance as "The Matrix" (1 movie)
3. **Select episode**: Returns the episode at the group's current position for this channel

### Sequential Episode Ordering

Episodes play in season/episode order per channel, not randomly:

- **Position tracking**: `series_positions` table stores `(channel_number, group_key) → position`
- **Initial position**: Deterministic from `md5(channel:group_key)` — stable across launches
- **Advancement**: Position increments when the playback engine confirms content played (in `tune_to()`)
- **Wrap-around**: `position % len(items)` — handles episodes added/deleted gracefully

### Schedule Calculation

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

Commercial time is distributed evenly across all breaks (including end). Every commercial break includes an info bumper (5-8 seconds) showing current program and next 2 upcoming.

### Commercial Selection

Deterministic RNG seeded by: `seed + (channel * 10000) + slot_number + break_index`. Same break always gets same commercials. Uses binary search on a pre-sorted duration list for large pools (1000+).

### Continuous Mode (Music/MTV)

Channels with `commercial_ratio: 0.0` bypass the slot-based engine:
1. Pool sorted by content ID, shuffled with seed `seed + channel_number`
2. Position = `elapsed_from_epoch % total_playlist_duration` (loops forever)
3. Same time = same video at same position (deterministic)

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

Status workflow: `scanned → identified → transcoded → ready`

## Running

```bash
cd L:\CableTV_Sim\app

# Standalone
python -m cabletv start              # Fullscreen
python -m cabletv start --windowed   # Windowed

# Server mode
python -m cabletv start --server     # Headless (API + generators only)
python -m cabletv start --server -w  # Server + TV window

# Remote mode
python -m cabletv start --remote -w  # Discovers server, opens video window

# Content management
python -m cabletv stats
python -m cabletv schedule now
python -m cabletv content search "matrix"
python -m cabletv content search "matrix" -v
python -m cabletv content list                  # All ready content
python -m cabletv content list --type movie     # By type
python -m cabletv content list --status scanned # By status
python -m cabletv content show <id>
python -m cabletv content edit <id> --title "Title" --tags "drama,comedy"
python -m cabletv content edit <id> --type show --series "Name" --season 2 --episode 5
python -m cabletv content edit <id> --year 1995 --artist "Artist Name"
python -m cabletv content reset <id1> <id2>     # Reset to scanned
python -m cabletv content delete <id1> <id2>    # Delete from DB + disk
python -m cabletv schedule check-collisions     # Same content on two channels

# Ingest pipeline
python -m cabletv ingest scan
python -m cabletv ingest identify               # AI (Claude + TMDB)
python -m cabletv ingest identify --no-ai       # Regex + TMDB fallback
python -m cabletv ingest transcode              # Or --skip
python -m cabletv ingest analyze                # Or --skip
python -m cabletv ingest all                    # Full pipeline
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze

# Guide / Weather
python -m cabletv guide generate [--short]
python -m cabletv weather generate
```

`python -m cabletv start` is a **long-running blocking command**. Use `--windowed` for development.

## Content Ingest

### Pipeline Steps

1. **Scan** — discovers video files, adds to DB as `scanned`
2. **Identify** — AI (Claude + TMDB) identifies title, type, tags, series info
3. **Transcode** — normalizes to 640x480 4:3 MP4 (or `--skip`)
4. **Analyze** — black frame detection for natural commercial break points (or `--skip`)
5. **Validate** — final status check

Always show the user the full output of each step. For identify, paste the review table and wait for confirmation before proceeding.

### Content Types

- `movie`, `show`, `commercial`, `bumper`, `music`
- Music videos detected by path containing "music video", "music_video", or "music collection"

### Tags

Valid: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, music, mystery, romance, scifi, thriller, western, classic, disney, sitcom, sports

- AI adds "classic" bonus for pre-1980s content, "disney" bonus for Disney/Pixar
- Tags must match channel config or content won't appear on any channel

### Tag-Channel Map

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

Movies-only channels: Ch9, Ch32, Ch50, Ch52, Ch55.

### Post-Ingest Checks

```bash
python -m cabletv stats                     # Tag distribution
python -m cabletv schedule check-collisions  # Same content on two channels
```

### Channel Configuration

Edit `config.yaml` directly. Key fields: `number`, `name`, `tags`, `content_types` (movie/show), `commercial_ratio` (0.0–1.0).

## Network Mode (Server/Remote)

Multiple PCs on the same LAN viewing the same broadcast. The server is the **single source of truth** for all schedule decisions.

### How It Works

```
SERVER (--server):
  - Runs ScheduleEngine + ServerScheduleManager (consumed-slot tracking)
  - Web server + API endpoints + mDNS advertisement
  - Guide/weather segment generation
  - With --windowed: also opens mpv as a TV

REMOTE (--remote):
  - Discovers server via mDNS (or manual server_url)
  - All schedule queries go to server API (what_is_on, get_upcoming, etc.)
  - No local ScheduleEngine — server is single source of truth
  - Everything streams over HTTP — no SMB/network share needed
  - Content files via /media endpoint, guide/weather via segment API
  - Position advances sent to server (fire-and-forget)
  - Post-load seek recalculation compensates for API latency
```

### Server API Endpoints

```
/api/server/info                    → Server seed, channels, config
/api/server/what-is-on/<channel>    → Serialized NowPlaying (the main query)
/api/server/upcoming/<channel>      → Upcoming programs for info bumpers
/api/server/next-airing/<channel>   → Next time a series airs
/api/server/advance (POST)          → Position advance (consumed-slot tracking)
/api/server/positions               → All series positions
/api/server/time                    → Server clock for offset calculation
/api/server/guide-segment           → Current guide segment metadata + URL
/api/server/weather-segment         → Current weather segment metadata + URL
/media/<path>                       → Content/segment file streaming with range requests
```

### Config (config.yaml)

```yaml
network:
  mode: standalone         # "standalone" | "server" | "remote"
  server_url: ""           # Manual: "http://192.168.1.100:5000"
  server_name: "CableTV Server"
  discovery_timeout: 10
```

Remote mode requires only `server_url` (or mDNS auto-discovery). No network share or `content_root` needed — all content and segments stream over HTTP.

### Key Files

```
schedule/remote_provider.py  → Thin API client (no local engine)
schedule/server_manager.py   → Consumed-slot tracking, prevents double-advances
interface/server_api.py      → All /api/server/* endpoints + NowPlaying serialization
network/client.py            → Server connection, mDNS discovery, clock offset
network/discovery.py         → mDNS advertisement + discovery
network/segment_provider.py  → HttpSegmentProvider (fetches segments via server API)
```

### Dependencies

- **Server**: `pip install zeroconf`
- **Remote**: `pip install requests` (zeroconf optional for auto-discovery)

## Guide Channel (Ch14)

Prevue-style scrolling grid with promo clips. `guide/generator.py` renders segments in background, played by mpv with loop. Playback engine polls for new segments. Guide shares the playback ScheduleEngine so the grid matches what actually plays.

## Weather Channel (Ch26)

90s Weather Channel with 6 cycling pages (current, forecast, extended, almanac, hourly, radar). Uses Open-Meteo API (free, no key) and RainViewer for radar. Segments pre-rendered to `weather/` directory.

## IMPORTANT Rules

### Showing Output
Always run `python -m cabletv` commands via Bash and paste the full output. For ingest, wait for user confirmation between steps.

### Database Changes
**NEVER modify schema without telling the user first.** Back up `cabletv.db` before any migration. Never use ALTER TABLE RENAME with `foreign_keys=ON`.

### Key Design Decisions
1. No stored schedule — calculated from clock, fully deterministic
2. TCP IPC for mpv (port 9876)
3. 30-minute slot grid, content rounds up to fill slots
4. Break points from black frame detection
5. Relative paths in database
6. Two-tier selection — equal weight per series, not per episode
7. Sequential episodes — position advances only on actual playback
8. Guide shares playback ScheduleEngine (grid matches what actually plays)
9. Remote mode is a thin API client — server is single source of truth
10. Remote mode is pure HTTP — no SMB/network share required
