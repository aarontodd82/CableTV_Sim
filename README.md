# CableTV Simulator

25 channels of 1990s cable TV. You flip through channels and land mid-program, just like the real thing. There's a Prevue guide channel, a Weather Channel with radar, commercial breaks with info bumpers, and MTV plays music videos all day. Pull up a web remote on your phone. Hook a Raspberry Pi up to a CRT and it's 1996.

## Features

**25 Channels** — Full cable lineup from local broadcast to premium. Comedy Central, Nickelodeon, Sci-Fi Channel, HBO, MTV, the works. Each channel has its own genre mix. All configurable.

**Channel Surfing** — Switch channels and you're mid-scene, right where you'd be if it had been playing this whole time. Flip away, come back ten minutes later, it kept going without you.

**Preview Channel (Ch 14)** — Prevue Guide recreation. Scrolling program grid on the bottom, promo clips from upcoming shows on the top. Background music that ducks during promos.

**Weather Channel (Ch 26)** — Six cycling pages, classic Weather Channel look. Current conditions, today's forecast, 5-day outlook, almanac with moon phase, hourly forecast, and radar (RainViewer). Color-coded temps, hand-drawn weather icons, scrolling ticker. Data from Open-Meteo, free, no API key.

**Commercial Breaks** — Shows play in 30-minute slots with breaks at natural scene changes (found by black-frame detection). During gaps, info bumper overlays show what's on now and coming up next.

**MTV (Ch 25)** — Music videos back-to-back, no commercials, no time slots. OSD shows artist, title, and year.

**Web Remote** — `http://<ip>:5000` on your phone. Number pad, channel up/down, guide.

**Multi-Room** — One server, any number of remotes on the LAN. Everything streams over HTTP, no file shares. Auto-discovery via mDNS.

**AI Ingest** — Drop files in a folder. The pipeline scans them, identifies what they are (Claude + TMDB), transcodes to 4:3, and finds break points. All automatic.

**Pi + CRT** — Composite video out, hardware decode, keyboard and IR remote support.

## It's TV, Not a Media Player

There's no pause. No rewind. No "play from beginning." You can't pick what's on — you turn it on and see what's playing. Tune into a movie halfway through? That's where you are. Flip away during a commercial and forget what you were watching? Happens. Miss the end of something good because you had to leave the room? Tough.

That's on purpose. The whole point of 90s cable wasn't having control — it was flipping through channels, stumbling into something halfway through, and deciding if you're staying. That's what this does.

## No Stored Schedule

There's no schedule file, no database of what plays when, no playlist. The entire schedule is calculated from the clock on the fly. Same clock + same content + same seed = same output. Always.

What that gets you:

- **Devices just agree.** Two machines with the same content and the same seed will play the same thing at the same time without talking to each other. The clock is the sync.
- **Nothing to break.** Restart the app, reboot the Pi, unplug it for a week. Come back and the schedule is right where it should be. It's just math.
- **The guide is always right.** The Preview Channel grid is generated from the same function the playback engine uses. What it says is on *is* what's on.
- **Multi-room for free.** Server mode exists for shared episode tracking, but standalone machines with the same content are already in sync.

Content gets picked in two tiers so every series gets a fair shot — a show with 260 episodes has the same chance of being scheduled as a single movie. Episodes play in order and only advance when you actually watch them.

## Channel Lineup

| Ch | Name | What's On |
|----|------|-----------|
| 3 | WCTV | Drama, Comedy, Family |
| 5 | Channel 5 | Action, Adventure |
| 7 | WPBS | Documentary, Educational |
| 9 | TV 9 Movies | Sci-Fi, Action, Drama, Comedy |
| **14** | **Preview Channel** | **Scrolling guide + promos** |
| 15 | Comedy Central | Comedy, Sitcom |
| 16 | A&E | Crime, Mystery, Drama |
| 17 | USA Network | Action, Crime |
| 18 | Disney Channel | Disney, Family |
| 20 | Cartoon Network | Animation, Kids |
| 22 | TBS | Comedy, Family |
| 24 | Sci-Fi Channel | Sci-Fi, Fantasy |
| 25 | MTV | Music Videos (continuous) |
| **26** | **Weather Channel** | **Live weather + radar** |
| 27 | Discovery | Documentary, History |
| 28 | Nickelodeon | Kids, Animation |
| 32 | AMC | Classic Movies |
| 34 | TV Land | Classic, Sitcom |
| 36 | Hallmark | Romance |
| 38 | The Arena | Game Show, Sports |
| 40 | Fright TV | Horror, Thriller |
| 42 | Western Frontier | Western |
| 44 | TNT | Action, Drama |
| 50 | HBO | Premium Movies |
| 52 | Cinemax | Premium Movies |
| 55 | TCM | Classic Movies |

Edit `config.yaml` to change the lineup.

## Getting Started

### You'll Need

- **Python 3.10+** — [python.org](https://python.org)
- **ffmpeg & ffprobe** — [ffmpeg.org](https://ffmpeg.org), in PATH
- **mpv** — [mpv.io](https://mpv.io), in PATH

### Install

```bash
cd app
pip install -r requirements.txt
```

### Add Content

Put video files in `content/originals/` (any folder structure). Commercials go in `commercials/originals/`.

Run the ingest pipeline:

```bash
# The works — AI identification, transcoding, break detection
python -m cabletv ingest all

# Quick — regex identification, skip transcoding and analysis
python -m cabletv ingest all --no-ai --skip-transcode --skip-analyze
```

### Go

```bash
python -m cabletv start              # Fullscreen
python -m cabletv start --windowed   # Windowed
```

Web remote is at `http://localhost:5000`.

## Controls

| Key | What It Does |
|-----|--------|
| Up / Down | Channel up / down |
| Left / Right | Volume down / up |
| 0–9 | Punch in a channel (e.g. `1` `5` for Ch 15) |
| M | Mute |
| I | Info overlay |
| Q | Quit |

## Content Ingest

The pipeline takes raw video files and gets them ready for broadcast.

### Stages

| Stage | What It Does |
|-------|-------------|
| **Scan** | Finds video files, deduplicates by hash |
| **Identify** | Figures out what the file is — AI (Claude + TMDB) or regex fallback |
| **Transcode** | Normalizes to 640x480 4:3 |
| **Analyze** | Black-frame detection to find natural break points |
| **Validate** | Marks it ready for air |

### Running Individual Stages

```bash
python -m cabletv ingest scan
python -m cabletv ingest identify          # AI + TMDB
python -m cabletv ingest identify --no-ai  # Regex + TMDB
python -m cabletv ingest transcode
python -m cabletv ingest analyze
```

### Content Types

- **movie** — Films
- **show** — TV episodes
- **commercial** — Short clips for breaks
- **bumper** — Very short clips
- **music** — Music videos (auto-detected by folder name)

### Tags

Tags control which channels content shows up on:

`action` `adventure` `animation` `comedy` `crime` `documentary` `drama` `educational` `family` `fantasy` `gameshow` `history` `horror` `kids` `music` `mystery` `romance` `scifi` `sitcom` `sports` `thriller` `western` `classic` `disney`

### After Ingest

```bash
python -m cabletv stats                      # See what you've got
python -m cabletv schedule check-collisions  # Same content on two channels?
```

## Content Management

```bash
python -m cabletv content list                       # Everything
python -m cabletv content list --type movie           # Just movies
python -m cabletv content search "matrix"             # Find something
python -m cabletv content search "matrix" -v          # With details
python -m cabletv content show <id>                   # Full info

python -m cabletv content edit <id> --title "Title" --tags "drama,comedy"
python -m cabletv content edit <id> --type show --series "Name" --season 2 --episode 5

python -m cabletv content reset <id>                  # Re-identify
python -m cabletv content delete <id>                 # Gone
```

## Schedule

```bash
python -m cabletv schedule now                        # What's on right now
python -m cabletv schedule show --channel 5 --hours 6 # One channel
python -m cabletv schedule check-collisions           # Duplicates
python -m cabletv stats                               # Counts and tags
```

## Multi-Room

One server, multiple TVs, same broadcast.

### Server

```bash
python -m cabletv start --server              # Headless — API only
python -m cabletv start --server --windowed   # Server with a TV window too
```

### Remote

```bash
python -m cabletv start --remote --windowed   # Finds the server automatically
```

Or point it manually:

```yaml
# config.yaml
network:
  mode: remote
  server_url: "http://192.168.1.100:5000"
```

Remotes stream everything over HTTP — video, guide segments, weather segments. No shared folders, no network drives. The server makes all the schedule decisions; remotes just play what they're told.

You'll need `pip install zeroconf` for auto-discovery.

## Guide & Weather

Both auto-generate during `start`. To make them manually:

```bash
python -m cabletv guide generate              # 10-minute segment
python -m cabletv guide generate --short      # Quick 2-minute test
python -m cabletv weather generate
```

## Configuration

Everything's in `config.yaml`.

### Schedule

```yaml
schedule:
  epoch: "2024-01-01T00:00:00"   # Anchor point
  slot_duration: 30               # Minutes per slot
  seed: 42                        # Different seed = different schedule
```

### Channels

```yaml
channels:
  - number: 15
    name: "Comedy Central"
    tags: [comedy, sitcom]
    content_types: [show]
    commercial_ratio: 1.0          # 0.0 = continuous, no commercials
```

### Playback

```yaml
playback:
  default_channel: 3
  osd_duration: 2.0
  overscan: 2.5                   # CRT overscan compensation (%)
  bumper_music: "path/to/music.mp3"
```

### Guide

```yaml
guide:
  enabled: true
  channel_number: 14
  scroll_speed: 3.0
  background_music: "path/to/prevue-music.mp3"
```

### Weather

```yaml
weather:
  enabled: true
  channel_number: 26
  latitude: 35.3965
  longitude: -79.0028
  location_name: "Lillington, NC"
  background_music: "path/to/smooth-jazz.mp3"
  radar_enabled: true
  units: imperial
```

### Ingest

```yaml
ingest:
  tmdb_api_key: ""                # Free at themoviedb.org
  anthropic_api_key: ""           # Or set ANTHROPIC_API_KEY env var
  widescreen_crop: 7              # % to crop from 16:9 sides
```

## Raspberry Pi

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg mpv
cd app
pip3 install -r requirements.txt
python3 -m cabletv start
```

It detects the Pi and sets up composite output with hardware decode automatically. Web remote from another device at `http://<pi-ip>:5000`.

If you put your content on a USB drive with "cabletv" in the name, it'll find it.

## Project Structure

```
CableTV_Sim/
├── app/cabletv/                # The app
│   ├── schedule/               # Scheduling engine
│   ├── playback/               # mpv, channel switching, OSD
│   ├── guide/                  # Prevue guide renderer
│   ├── weather/                # Weather Channel renderer
│   ├── network/                # mDNS, HTTP streaming
│   ├── interface/              # Web remote, server API
│   ├── ingest/                 # Content pipeline
│   └── utils/                  # ffmpeg, time helpers
├── config.yaml
├── cabletv.db                  # SQLite (auto-created)
├── content/originals/          # Your video files
├── content/normalized/         # Transcoded output
├── commercials/
├── guide/                      # Generated segments
└── weather/                    # Generated segments
```

## Troubleshooting

**Nothing on any channels** — `python -m cabletv stats`. Content tags need to match channel tags in `config.yaml`.

**mpv won't start** — Make sure `mpv --version` and `ffmpeg -version` work.

**Remote can't find server** — `pip install zeroconf` on both sides, or just set `server_url` in the config.

**Transcoding takes forever** — Yeah, that's normal for big libraries. `--skip-transcode` uses originals as-is.

## License

Personal/educational use.
