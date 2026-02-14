# CableTV Simulator

Recreate the authentic experience of 1990s cable TV with 20 channels of scheduled programming on a CRT television.

## Features

- **20 Channels** - Configure channels with different genres and content types
- **Deterministic Scheduling** - Same content library = same schedule every time
- **Web Remote Control** - Change channels from your phone or any browser
- **Cross-Platform** - Develop on Windows, deploy on Raspberry Pi
- **Automatic Content Ingest** - Drop files in a folder, run one command

## Quick Start

### Prerequisites

1. **Python 3.10+** - [Download](https://python.org)
2. **ffmpeg & ffprobe** - [Download](https://ffmpeg.org), add to PATH
3. **mpv** - [Download](https://mpv.io), add to PATH

### Installation

```bash
cd C:\Users\aaron\OneDrive\Documents\CableTV_Sim\app
pip install -r requirements.txt
```

### Add Content

1. Copy video files to `content/originals/` (any folder structure)
2. Run the ingest pipeline:

```bash
# Quick ingest (no TMDB, no transcoding, no analysis)
python -m cabletv ingest all --skip-tmdb --skip-transcode --skip-analyze

# Full ingest (with TMDB lookup and transcoding)
python -m cabletv ingest all
```

### Start the System

```bash
# Full system (fullscreen mpv + web remote)
python -m cabletv start

# Windowed mode for testing
python -m cabletv start --windowed

# Without web interface
python -m cabletv start --no-web
```

### Use the Remote

Open http://localhost:5000 in any browser. Use:
- Number pad to enter channel numbers
- CH up/down buttons
- Click channels in the list
- Keyboard: arrow keys, number keys

## Commands Reference

### System

```bash
python -m cabletv start              # Start full system
python -m cabletv start --windowed   # Windowed mode
python -m cabletv stats              # Show statistics
```

### Content Ingest

```bash
python -m cabletv ingest scan        # Find new video files
python -m cabletv ingest identify    # TMDB metadata lookup (interactive)
python -m cabletv ingest identify --auto   # Auto-accept high confidence matches
python -m cabletv ingest identify --skip   # Skip TMDB entirely
python -m cabletv ingest transcode   # Convert to 640x480 4:3
python -m cabletv ingest transcode --skip  # Use original files
python -m cabletv ingest analyze     # Detect commercial break points
python -m cabletv ingest analyze --skip    # Skip analysis
python -m cabletv ingest all         # Run complete pipeline
python -m cabletv ingest status      # Show pipeline status
```

### Content Management

```bash
python -m cabletv content list               # List all content
python -m cabletv content list --type movie  # Filter by type
python -m cabletv content list --status ready # Filter by status
python -m cabletv content show 1             # Show details for content ID 1
python -m cabletv content tag 1 comedy       # Add tag to content
python -m cabletv content tag 1 drama --remove # Remove tag
```

### Schedule

```bash
python -m cabletv schedule now       # What's on all channels right now
python -m cabletv schedule show      # Full schedule display
python -m cabletv schedule show --channel 5 --hours 4  # Specific channel
python -m cabletv schedule check-collisions  # Find duplicate content
```

## Configuration

Edit `config.yaml` to customize:

### Channels

```yaml
channels:
  - number: 2
    name: "Comedy Central"
    tags: ["comedy", "sitcom"]
    content_types: ["show", "movie"]

  - number: 5
    name: "Sci-Fi Network"
    tags: ["scifi", "science-fiction"]
    content_types: ["movie", "show"]
```

### Schedule Settings

```yaml
schedule:
  epoch: "2024-01-01T00:00:00"  # Reference point for scheduling
  slot_duration: 30             # Minutes per slot
  seed: 42                      # Random seed (change for different schedule)
```

### TMDB Integration

Get a free API key from https://www.themoviedb.org/settings/api

```yaml
ingest:
  tmdb_api_key: "your_api_key_here"
```

## Directory Structure

```
CableTV_Sim/
├── app/cabletv/          # Application code
├── config.yaml           # Configuration
├── cabletv.db           # Database (created automatically)
├── content/
│   ├── originals/       # Put your video files here
│   └── normalized/      # Transcoded files go here
├── commercials/
│   ├── originals/       # Commercial files
│   └── normalized/
└── logs/
```

## Adding Content Workflow

1. **Copy files** to `content/originals/` - any folder structure works
2. **Run ingest**: `python -m cabletv ingest all`
3. **Done!** Content appears in schedule automatically

### Supported Video Formats

.mp4, .mkv, .avi, .mov, .wmv, .flv, .webm, .m4v, .mpg, .mpeg, .ts, .m2ts

### Content Types

- **movie** - Full-length films
- **show** - TV episodes (detected from S01E01 patterns)
- **commercial** - Short clips for breaks (place in `commercials/originals/`)

## How Scheduling Works

The schedule is **fully deterministic** - calculated on-the-fly from the master clock, not stored. Given the same content library, epoch, and seed, the same channel always shows the same thing at the same time.

- **30-minute grid**: All content snaps to 30-minute slots
- **Commercial breaks**: Inserted at detected break points within shows, plus end-of-slot padding
- **Time passes**: Flip away and back - you're at the correct position (±1 second)
- **Multi-device sync**: Two devices with same config show identical content

## Raspberry Pi Deployment

1. Install dependencies:
```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg mpv
```

2. Copy the entire CableTV_Sim folder to the Pi

3. Install Python packages:
```bash
cd /path/to/CableTV_Sim/app
pip3 install -r requirements.txt
```

4. Run:
```bash
python3 -m cabletv start
```

5. Access web remote from another device at `http://pi-ip-address:5000`

## Troubleshooting

### "No content available" on channels
- Run `python -m cabletv content list` to see if content exists
- Check that content has tags matching channel configuration
- Run `python -m cabletv ingest status` to see pipeline status

### mpv doesn't start
- Verify mpv is installed: `mpv --version`
- Check it's in PATH
- Try running mpv manually first

### Transcode is slow
- This is normal - run on a fast PC, not the Pi
- Use `--skip-transcode` to use original files

### TMDB not finding matches
- Check your API key in config.yaml
- Use `--skip` to skip TMDB if not needed
- Matches depend on filename - rename files to "Title (Year)" format

## License

Personal/educational use.
