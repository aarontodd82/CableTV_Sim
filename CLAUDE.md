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

app/cabletv/
├── schedule/
│   ├── engine.py        → Timeline building, what_is_on()
│   └── commercials.py   → Commercial selection, slot breakdown
├── playback/
│   ├── engine.py        → Channel switching, segment transitions
│   └── mpv_control.py   → mpv TCP IPC (port 9876)
├── ingest/              → 5-stage pipeline
├── interface/           → Flask web API + remote UI
└── utils/               → ffmpeg, time calculations
```

## Core Algorithm

### Schedule Calculation (on every query)

```python
what_is_on(channel=5, when=now)
  1. Calculate slot number from epoch + current time
  2. Walk backward to find content block start (handles multi-slot content)
  3. Fetch break points from database
  4. Build timeline: [content, commercial, content, commercial, ..., end padding]
  5. Find which segment we're in based on elapsed time
  6. Return: file path + seek position (content or commercial)
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

When commercials can't perfectly fill a break, the remaining time becomes an info bumper:
- **Under 3 seconds**: black screen (barely noticeable)
- **3+ seconds**: black screen with OSD mini-guide showing current program and next 2 upcoming programs with start times
- Variable duration — fills whatever gap commercials leave
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

## Database Schema

```sql
content: id, title, content_type, duration_seconds, original_path,
         normalized_path, file_hash, status, ...
tags: id, name, description
content_tags: content_id, tag_id
break_points: id, content_id, timestamp_seconds, confidence
```

Status workflow: scanned → identified → transcoded → ready

## Running

```bash
cd C:\Users\Aaron\Documents\CableTV_Sim\app
python -m cabletv start           # Full system
python -m cabletv start --windowed
python -m cabletv stats
python -m cabletv schedule now
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze
```

## Content Ingest Workflow

### Adding New Content

When the user adds new video files and wants them ingested, follow this workflow step by step. **The user must see and confirm the output at each step.** Always paste the full CLI output in your response — never summarize or skip it.

#### Step 1: Scan

```bash
cd C:\Users\Aaron\Documents\CableTV_Sim\app
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
| **Schedule** | Slot-based (30min grid), one item per slot | Continuous playlist — loops shuffled pool, multiple videos per slot |
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

### Files Involved

- `schedule/engine.py` — `_what_is_on_continuous()` method, `ScheduleEntry.artist`/`.year` fields
- `playback/engine.py` — `_show_music_osd()`, `_music_end_timer`
- `ingest/scanner.py` — music detection in `detect_content_type()`
- `ingest/ai_identifier.py` — `MUSIC_SYSTEM_PROMPT`, music batch routing
- `db.py` — `artist` column, `'music'` in content_type CHECK, migration in `_run_migrations()`

## Implementation Status

### Complete
- Deterministic schedule engine with timeline-based commercial breaks
- 5-stage ingest pipeline (scan, identify, transcode, analyze, register)
- AI-powered content identification (Claude API + TMDB tool use)
- Smart commercial selection (fits duration, handles gaps)
- Variable info bumpers (mini-guide OSD in commercial gaps >= 3s)
- mpv playback with segment transitions (content, commercial, info bumper)
- Web remote control
- 25 channels configured (real cable names: MTV, Comedy Central, Sci-Fi Channel, HBO, Disney Channel, etc.)
- Content edit/reset CLI commands for fixing misidentifications
- Music video channel (Ch25 — MTV) with continuous playlist, artist OSD, no commercials
- "Classic" bonus tag auto-assigned by AI for pre-1980s content
- "Disney" bonus tag auto-assigned by AI for Disney/Pixar content

### Not Implemented
- Guide channel (Prevue-style scrolling grid)
- GPIO/IR input on Pi
- Standby video (currently OSD text only)
- Promo bumpers ("Tonight at 7:30...") - foundation exists via `what_is_on(channel, when)`

## Known Limitations

1. **100-slot lookback limit** - Content >50 hours may not schedule correctly (walks back max 100 slots)
2. **Callbacks fired inside lock** - Don't call `tune_to()` from callbacks (deadlock risk)

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
4. **Commercials cut off**: Fixed - now uses smart fitting algorithm
5. **Info bumper not showing**: Only displays when gap is >= 3 seconds
