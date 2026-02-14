# CableTV Simulator - Claude Internal Knowledge

## Project Summary

CableTV Simulator recreates 1990s cable TV with 20 channels of deterministically scheduled content. Development on Windows, deployment on Raspberry Pi + CRT.

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
7:00-9:24   = Commercial break #0 (2:24)
9:24-9:32   = "Coming Up Next" bumper (8s)
9:32-16:32  = Show (seek 7:00-14:00)
16:32-18:56 = Commercial break #1 (2:24)
18:56-19:04 = "Coming Up Next" bumper (8s)
19:04-27:04 = Show (seek 14:00-22:00)
27:04-30:00 = Commercial break #2 / end padding (2:56)
```

Commercial time is distributed evenly across all breaks (including end).

### "Coming Up Next" Bumpers

- 8-second black screen with OSD showing next program title
- Up to 3 bumpers per 30-min slot
- Placed at end of selected commercial breaks (deterministic)
- Never placed in end padding (final break)
- Never twice in the same break

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
- Valid tags: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, music, mystery, romance, scifi, thriller, war, western, classic, sitcom, cult, sports
- Tags must match channel config tags or content won't appear on any channel.

### Post-Ingest Verification

After content finishes the pipeline (all stages complete), always run these checks and show the user the output:

1. **`python -m cabletv stats`** — verify tag distribution makes sense. If a tag has 0 content but a channel uses it, flag it to the user.
2. **`python -m cabletv schedule check-collisions`** — check the same content isn't on two channels at once. Show results.

### Tag-Channel Alignment

Content ONLY appears on a channel if it has at least one tag matching that channel's tags AND a matching content_type. After identification, check that no content is "orphaned" (has tags that don't match any channel).

Current channel tag coverage:
- action: Ch3, 5, 12, 46, 49, 62
- adventure: Ch12, 46
- animation: Ch27, 30, 33
- comedy: Ch3, 5, 8, 15, 27, 52
- crime: Ch36, 55
- classic: Ch43, 62
- documentary: Ch40, 44
- drama: Ch3, 5, 8, 18, 43, 52, 58, 62
- educational: Ch44
- family: Ch8, 27, 30, 33
- fantasy: Ch24
- gameshow: Ch42
- history: Ch40, 49
- horror: Ch21, 58
- kids: Ch30, 33
- music: Ch35
- mystery: Ch36, 55
- romance: Ch18, 43, 52
- scifi: Ch24, 62
- sitcom: Ch15
- sports: Ch42
- thriller: Ch5, 21, 36, 55, 58
- war: Ch12, 49
- western: Ch46

Tags with NO channel: cult. Content with only this tag will never appear. If the AI assigns only "cult", add a covered tag too or flag it to the user.

Movies-only channels: Ch5 (Five Star Movies), Ch62 (Cinema Showcase). Don't let shows end up with tags that only match these channels unless they're actually movies.

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

## Implementation Status

### Complete
- Deterministic schedule engine with timeline-based commercial breaks
- 5-stage ingest pipeline (scan, identify, transcode, analyze, register)
- AI-powered content identification (Claude API + TMDB tool use)
- Smart commercial selection (fits duration, handles gaps)
- "Coming Up Next" bumpers (8s, up to 3 per slot, deterministic placement)
- mpv playback with segment transitions (content, commercial, up_next)
- Web remote control
- 20 channels configured
- Content edit/reset CLI commands for fixing misidentifications

### Not Implemented
- Guide channel (Prevue-style scrolling grid)
- GPIO/IR input on Pi
- Standby video (currently OSD text only)
- Promo bumpers ("Tonight at 7:30...") - foundation exists via `what_is_on(channel, when)`

## Known Limitations

1. **100-slot lookback limit** - Content >50 hours may not schedule correctly (walks back max 100 slots)
2. **Callbacks fired inside lock** - Don't call `tune_to()` from callbacks (deadlock risk)

## Common Issues

1. **Content not showing**: Check tags match channel config
2. **mpv won't start**: Verify mpv in PATH
3. **Schedule seems wrong**: Remember it's deterministic - same time = same content
4. **Commercials cut off**: Fixed - now uses smart fitting algorithm
5. **Up_next bumpers missing**: Only added when there's enough commercial time (>8 seconds)
