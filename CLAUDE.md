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
cd C:\Users\aaron\OneDrive\Documents\CableTV_Sim\app
python -m cabletv start           # Full system
python -m cabletv start --windowed
python -m cabletv stats
python -m cabletv schedule now
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze
```

## Implementation Status

### Complete
- Deterministic schedule engine with timeline-based commercial breaks
- 5-stage ingest pipeline (scan, identify, transcode, analyze, register)
- Smart commercial selection (fits duration, handles gaps)
- "Coming Up Next" bumpers (8s, up to 3 per slot, deterministic placement)
- mpv playback with segment transitions (content, commercial, up_next)
- Web remote control
- 20 channels configured

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
