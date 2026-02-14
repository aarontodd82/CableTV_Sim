# CableTV Simulator — Build & Implementation Guide

## Implementation Status

**Last Updated**: February 2026

### Completed (Phase 1 MVP)
- [x] Directory structure and project layout
- [x] Platform abstraction (`platform.py`) - cross-platform path handling, mpv TCP IPC
- [x] Configuration system (`config.py`) - YAML loading with dataclasses
- [x] Database layer (`db.py`) - SQLite with WAL mode, full schema
- [x] Default `config.yaml` with 20 channels
- [x] FFmpeg utilities (`utils/ffmpeg.py`) - probe_file, compute_file_hash
- [x] Time utilities (`utils/time_utils.py`) - epoch math, slot calculations
- [x] Ingest Stage 1: Scanner (`ingest/scanner.py`) - find & probe files
- [x] Ingest Stage 2: Identifier (`ingest/identifier.py`) - TMDB lookup
- [x] Ingest Stage 3: Transcoder (`ingest/transcoder.py`) - 640x480 4:3 normalization
- [x] Ingest Stage 4: Analyzer (`ingest/analyzer.py`) - black-frame detection
- [x] Ingest Stage 5: Registrar (`ingest/registrar.py`) - validation & registration
- [x] Schedule engine (`schedule/engine.py`) - deterministic scheduling
- [x] mpv controller (`playback/mpv_control.py`) - TCP IPC wrapper
- [x] Playback engine (`playback/engine.py`) - channel switching, content timing
- [x] Web interface (`interface/web.py` + static files) - Flask API + remote UI
- [x] CLI entry point (`__main__.py`) - all commands implemented
- [x] Main startup (`main.py`) - system coordination
- [x] **Timeline-based commercial system** - builds complete schedule with content segments and commercial breaks at detected break points; fully deterministic based on master clock

### Not Yet Implemented (Phase 2+)
- [ ] Guide channel video rendering (Prevue-style)
- [ ] GPIO button input (Raspberry Pi)
- [ ] IR remote receiver
- [ ] Channel change sound effects
- [ ] Test content generator script
- [ ] systemd service for Pi auto-start
- [ ] **Promo bumpers** - "Tonight at 7:30, catch Terminator 2 on Channel 5"

### Known Limitations (MVP)
- Schedule algorithm searches back max 100 slots; content >50 hours may not schedule correctly

---

## Project Overview

Build a Raspberry Pi-powered cable TV simulator that recreates the authentic experience of flipping through 20 channels of scheduled programming on a CRT television. Content is sourced from a local hard drive, deterministically scheduled to a 30-minute grid, and played back with channel switching and OSD overlays. The schedule is computed on-the-fly from the master clock - no stored schedule, fully deterministic.

**Development happens on Windows. Deployment target is Raspberry Pi + CRT.**

The external hard drive is the single source of truth — application code, database, content, config all live on it. Plug it into any machine, point the app at the drive root, and it runs.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Drive & File Structure](#2-drive--file-structure)
3. [Configuration System](#3-configuration-system)
4. [Database Schema](#4-database-schema)
5. [Subsystem 1: Content Ingest Pipeline](#5-subsystem-1-content-ingest-pipeline)
6. [Subsystem 2: Schedule Engine](#6-subsystem-2-schedule-engine)
7. [Subsystem 3: Playback Engine](#7-subsystem-3-playback-engine)
8. [Subsystem 4: Channel Switch Interface](#8-subsystem-4-channel-switch-interface)
9. [Subsystem 5: Preview/Guide Channel](#9-subsystem-5-previewguide-channel)
10. [Subsystem 6: Display & Output](#10-subsystem-6-display--output)
11. [Platform Abstraction Layer](#11-platform-abstraction-layer)
12. [Dependencies & Installation](#12-dependencies--installation)
13. [Development Workflow](#13-development-workflow)
14. [MVP Scope & Phased Roadmap](#14-mvp-scope--phased-roadmap)
15. [Future Enhancements](#15-future-enhancements)
16. [Technical Notes & Gotchas](#16-technical-notes--gotchas)

---

## Grid Alignment with Real Time

The schedule grid automatically aligns with real-world clock times when the epoch is set to any midnight. With `epoch: "2024-01-01T00:00:00"` and 30-minute slots:

- All slot boundaries fall on :00 and :30 of each hour
- Content always starts at times like 7:00 PM, 7:30 PM, 8:00 PM
- This is because: midnight + (N × 30 minutes) = always :00 or :30

**Why this matters**: Enables future promo bumpers that say "Tonight at 7:30, catch Terminator 2 on Channel 5"

### Future Feature: Promo Bumpers

The foundation exists to implement time-aware promo bumpers:

1. **Query future schedule** - `what_is_on(channel, when)` already supports any future time
2. **Generate promo text** - "Tonight at 7:30" or "Tomorrow at 8:00"
3. **Select/generate bumper** - Could be pre-rendered videos or dynamic OSD overlays
4. **Schedule in timeline** - Insert promo bumpers during commercial breaks

**Implementation approach** (not yet built):
```python
def get_upcoming_highlight(channel: int, hours_ahead: int = 6) -> dict:
    """Find a notable upcoming show to promote."""
    # Query schedule for next N hours
    # Find content matching "highlight" criteria (movies, premieres, etc.)
    # Return: title, channel, start_time formatted as "Tonight at 7:30"
```

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    EXTERNAL HARD DRIVE                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────┐  │
│  │ Content  │  │ SQLite   │  │  Config  │  │  App   │  │
│  │ Library  │  │    DB    │  │  (YAML)  │  │  Code  │  │
│  └──────────┘  └──────────┘  └──────────┘  └────────┘  │
└─────────────────────────────────────────────────────────┘
         │              │              │             │
         ▼              ▼              ▼             ▼
┌─────────────────────────────────────────────────────────┐
│                   APPLICATION LAYER                      │
│                                                         │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────┐  │
│  │   Ingest    │───▶│   Schedule   │───▶│  Playback │  │
│  │  Pipeline   │    │    Engine    │    │   Engine  │  │
│  └─────────────┘    └──────────────┘    └───────────┘  │
│                            │                   │        │
│                            ▼                   ▼        │
│                     ┌──────────────┐    ┌───────────┐  │
│                     │   Preview    │    │  Channel  │  │
│                     │   Channel    │    │ Interface │  │
│                     └──────────────┘    └───────────┘  │
│                                                         │
│  ┌─────────────────────────────────────────────────────┐│
│  │              Platform Abstraction Layer              ││
│  │  (paths, display config, input handlers, player)    ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│   mpv (IPC socket)  │──▶ HDMI out ──▶ Converter ──▶ CRT
└─────────────────────┘
```

### Key Architectural Principles

- **Deterministic scheduling**: Given the same content library and epoch, the schedule is always identical. No persisted "play history" needed. Schedule is computed on-the-fly from wall-clock time.
- **Portable drive**: All state, config, content, and code on one external drive. Works on Windows or Linux with zero changes.
- **Relative paths only**: The database and config never store absolute paths. Everything is relative to the drive root.
- **Modular subsystems**: Each subsystem is an independent Python module with clean interfaces. They communicate via the DB and a lightweight internal message bus (or direct function calls for MVP).
- **Incremental ingest**: Adding new content never requires reprocessing existing content. The schedule engine dynamically incorporates new entries.
- **mpv as the universal player**: Controlled via JSON IPC socket. Handles video playback, seeking, OSD overlays, and aspect ratio.

---

## 2. Drive & File Structure

```
[DRIVE_ROOT]/
├── app/                           # Application code (this entire project)
│   ├── cabletv/                   # Main Python package
│   │   ├── __init__.py
│   │   ├── __main__.py            # Entry point: python -m cabletv
│   │   ├── config.py              # Config loader
│   │   ├── db.py                  # Database access layer
│   │   ├── platform.py            # Platform abstraction
│   │   ├── main.py                # System startup coordinator
│   │   ├── ingest/
│   │   │   ├── __init__.py
│   │   │   ├── scanner.py         # Stage 1: Find & probe files
│   │   │   ├── identifier.py      # Stage 2: TMDB lookup & tagging
│   │   │   ├── transcoder.py      # Stage 3: Normalize video
│   │   │   ├── analyzer.py        # Stage 4: Black-frame detection
│   │   │   └── registrar.py       # Stage 5: Write to DB
│   │   ├── schedule/
│   │   │   ├── __init__.py
│   │   │   └── engine.py          # Core schedule computation
│   │   ├── playback/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py          # Playback controller
│   │   │   └── mpv_control.py     # mpv IPC wrapper
│   │   ├── interface/
│   │   │   ├── __init__.py
│   │   │   ├── web.py             # Flask web UI (MVP remote)
│   │   │   └── static/            # Web UI assets
│   │   │       ├── index.html
│   │   │       ├── style.css
│   │   │       └── remote.js
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── ffmpeg.py          # ffmpeg/ffprobe wrapper functions
│   │       └── time_utils.py      # Epoch math, slot calculations
│   └── requirements.txt
├── config.yaml                    # Master configuration file
├── cabletv.db                     # SQLite database (created at runtime)
├── content/
│   ├── originals/                 # Raw source files (any format)
│   └── normalized/                # Transcoded 640x480 4:3 MP4s
├── commercials/
│   ├── originals/                 # Raw commercial files
│   └── normalized/                # Transcoded commercials
└── logs/
```

### Important Notes on File Organization

- Users drop raw content into `content/originals/` in any structure they want — flat, nested, whatever. The scanner finds everything recursively.
- Transcoded files go into `content/normalized/` with the same filename but .mp4 extension.
- The DB is the authority on what exists and where it is. The file system is just storage.
- Commercials follow the same pattern. Drop originals in, ingest normalizes them.

---

## 3. Configuration System

**File**: `[DRIVE_ROOT]/config.yaml`

The configuration is loaded via dataclasses in `config.py`. Key sections:

- **schedule**: epoch, slot_duration (30 min), seed for deterministic scheduling
- **channels**: list of channel definitions with number, name, tags, content_types
- **ingest**: TMDB API key, transcode resolution (640x480), bitrate settings
- **playback**: mpv IPC port (9876), OSD duration, default channel
- **web**: host (0.0.0.0), port (5000), debug mode

See `config.yaml` in the project root for the full default configuration.

---

## 4. Database Schema

**File**: `[DRIVE_ROOT]/cabletv.db` (SQLite with WAL mode)

### Tables

**content** - Main content registry
- id, title, content_type (movie/show/commercial/bumper)
- series_name, season, episode, year
- duration_seconds, original_path, normalized_path, file_hash
- tmdb_id, status (scanned → identified → transcoded → ready)
- width, height, aspect_ratio, codec
- created_at, updated_at

**tags** - Available genre/category tags
- id, name, description

**content_tags** - Many-to-many relationship
- content_id, tag_id

**break_points** - Commercial break timecodes
- id, content_id, timestamp_seconds, confidence

**ingest_log** - Processing history
- id, content_id, stage, status, message, created_at

---

## 5. Subsystem 1: Content Ingest Pipeline

The 5-stage pipeline is fully implemented:

### Stage 1: Scanner (`ingest/scanner.py`)
- Recursively finds video files (.mp4, .mkv, .avi, etc.)
- Probes each with ffprobe for duration, resolution, codec
- Computes SHA256 hash for duplicate detection
- Detects content type from path/filename patterns
- Inserts into DB with status='scanned'

### Stage 2: Identifier (`ingest/identifier.py`)
- Parses filenames for title/season/episode/year
- Queries TMDB API for metadata
- Maps TMDB genres to tags
- Interactive mode for confirmation, auto mode for high-confidence matches
- Updates status to 'identified'

### Stage 3: Transcoder (`ingest/transcoder.py`)
- Builds ffmpeg command for 640x480 4:3 output
- Handles aspect ratio conversion (letterboxes widescreen)
- Adds keyframes every 30 frames for fast seeking
- Updates status to 'transcoded'

### Stage 4: Analyzer (`ingest/analyzer.py`)
- Black-frame detection using ffmpeg blackdetect filter
- Finds natural commercial break points
- Stores break points in DB
- Updates status to 'ready' (skipped for short content)

### Stage 5: Registrar (`ingest/registrar.py`)
- Final validation of content
- Verifies files exist and are playable
- Provides pipeline status overview

---

## 6. Subsystem 2: Schedule Engine

**File**: `schedule/engine.py`

The schedule is computed deterministically from:
1. Epoch (fixed start point from config)
2. Current wall-clock time
3. Content library (from DB)
4. Channel definitions (from config)
5. Seed value (from config)

### Key Methods

- `get_channel_pool(channel_config)` - Get content matching channel tags
- `what_is_on(channel_number, when)` - What's playing at a specific time
- `get_guide_data(start_time, hours)` - TV guide data for multiple channels
- `check_collisions()` - Find same content on multiple channels

### How It Works

1. Content is assigned to channels based on matching tags
2. A deterministic shuffle creates the playback order
3. Content is laid out on a 30-minute grid
4. The engine can calculate what's playing at any point in time

### Timeline-Based Commercial System

**Files**: `schedule/engine.py`, `schedule/commercials.py`

**Everything is scheduled, nothing is injected.** The schedule engine builds a complete timeline for each content block that includes content segments and commercial breaks at detected break points.

#### How It Works

1. **Timeline Building** (`build_content_timeline()`):
   - Takes content duration, break points (from database), and total slot time
   - Distributes total commercial time evenly across all breaks (including end padding)
   - Creates alternating content/commercial `TimelineSegment` objects

   Example for a 22-min show with breaks at 7:00 and 14:00 in a 30-min slot:
   ```
   0:00-7:00   = Show (seek 0:00-7:00)
   7:00-9:40   = Commercial break #0
   9:40-16:40  = Show (seek 7:00-14:00)
   16:40-19:20 = Commercial break #1
   19:20-27:20 = Show (seek 14:00-22:00)
   27:20-30:00 = Commercial break #2 (end padding)
   ```

2. **What's On** (`what_is_on()`):
   - Finds the content block for the current time
   - Fetches break points from database (`get_break_points()`)
   - Builds the timeline
   - Finds which segment we're in (`find_current_segment()`)
   - Returns exact seek position in the correct file (content or commercial)

3. **Deterministic Commercial Selection** (`get_current_commercial()`):
   - Uses seeded random (channel + slot + break_index + seed)
   - Same time = same commercial at same position, always

4. **Playback Transitions**:
   - When a segment ends, timer fires and re-tunes
   - Next segment is calculated deterministically
   - Seamless content → commercial → content flow

---

## 7. Subsystem 3: Playback Engine

### mpv Controller (`playback/mpv_control.py`)
- TCP IPC connection on port 9876 (cross-platform)
- Methods: start(), play_file(), seek(), pause(), show_osd_message(), stop(), shutdown()
- Handles mpv process lifecycle

### Playback Engine (`playback/engine.py`)
- Coordinates schedule engine with mpv
- `tune_to(channel)` - Switch to a channel, seek to correct position
- `channel_up()` / `channel_down()` - Navigate channels
- Schedules content end timers for automatic transitions
- Shows channel OSD on switch

---

## 8. Subsystem 4: Channel Switch Interface

### Web Interface (`interface/web.py`)
Flask API endpoints:
- `POST /api/channel/<n>` - Tune to channel
- `POST /api/channel/up` - Channel up
- `POST /api/channel/down` - Channel down
- `GET /api/status` - Current playback status
- `GET /api/channels` - List all channels
- `GET /api/guide` - TV guide data

### Web Remote (`interface/static/`)
- Modern responsive remote control UI
- Number pad with auto-submit
- Channel up/down buttons
- Channel list with quick-tune
- Live status display with progress bar

---

## 9-16. [See original document for remaining sections]

These sections cover:
- Preview/Guide Channel (not yet implemented)
- Display & Output configuration
- Platform Abstraction Layer
- Dependencies & Installation
- Development Workflow
- MVP Scope & Phased Roadmap
- Future Enhancements
- Technical Notes & Gotchas

---

## Quick Reference: CLI Commands

```bash
# Start the full system
python -m cabletv start
python -m cabletv start --windowed    # Windowed mode for development
python -m cabletv start --no-web      # Without web interface

# Ingest pipeline
python -m cabletv ingest scan         # Find new files
python -m cabletv ingest identify     # TMDB lookup (interactive)
python -m cabletv ingest identify --auto   # Auto-accept matches
python -m cabletv ingest identify --skip   # Skip TMDB
python -m cabletv ingest transcode    # Convert to 640x480
python -m cabletv ingest transcode --skip  # Use originals
python -m cabletv ingest analyze      # Black-frame detection
python -m cabletv ingest analyze --skip    # Skip analysis
python -m cabletv ingest all          # Run full pipeline
python -m cabletv ingest status       # Pipeline status

# Content management
python -m cabletv content list
python -m cabletv content list --type movie
python -m cabletv content show <id>
python -m cabletv content tag <id> <tag>
python -m cabletv content tag <id> <tag> --remove

# Schedule
python -m cabletv schedule now        # What's on all channels
python -m cabletv schedule show       # Full schedule
python -m cabletv schedule show --channel 5 --hours 4
python -m cabletv schedule check-collisions

# Statistics
python -m cabletv stats
```

---

## Adding Content Workflow

1. Copy video files to `content/originals/` (any structure)
2. Run `python -m cabletv ingest all` (or individual stages)
3. Content automatically appears in schedule
4. No restart needed - schedule engine picks up new content

For quick testing without transcoding:
```bash
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze
```
