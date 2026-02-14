"""CLI entry point for CableTV Simulator."""

import argparse
import sys
from datetime import datetime
from pathlib import Path


def cmd_start(args):
    """Start the CableTV system."""
    from .main import start_system
    start_system(fullscreen=not args.windowed, no_web=args.no_web)


def cmd_ingest_scan(args):
    """Scan content directories."""
    from .config import load_config
    from .db import init_database
    from .ingest.scanner import scan_all

    init_database()
    config = load_config()
    stats = scan_all(config, verbose=True)
    print(f"\nScan complete: {stats['added']} added, {stats['skipped']} skipped, {stats['errors']} errors")


def cmd_ingest_identify(args):
    """Identify content via TMDB."""
    from .config import load_config
    from .db import init_database
    from .ingest.identifier import identify_content, skip_identification

    init_database()
    config = load_config()

    if args.skip:
        skip_identification(verbose=True)
    else:
        identify_content(config, auto=args.auto, verbose=True)


def cmd_ingest_transcode(args):
    """Transcode content to 640x480."""
    from .config import load_config
    from .db import init_database
    from .ingest.transcoder import transcode_all, skip_transcode

    init_database()
    config = load_config()

    if args.skip:
        skip_transcode(verbose=True)
    else:
        transcode_all(config, verbose=True, force=args.force)


def cmd_ingest_analyze(args):
    """Analyze content for break points."""
    from .config import load_config
    from .db import init_database
    from .ingest.analyzer import analyze_all, skip_analysis

    init_database()
    config = load_config()

    if args.skip:
        skip_analysis(verbose=True)
    else:
        analyze_all(config, verbose=True, force=args.force)


def cmd_ingest_all(args):
    """Run full ingest pipeline."""
    from .config import load_config
    from .db import init_database
    from .ingest.registrar import run_full_pipeline

    init_database()
    config = load_config()
    run_full_pipeline(
        config,
        auto=args.auto,
        skip_tmdb=args.skip_tmdb,
        skip_transcode=args.skip_transcode,
        skip_analyze=args.skip_analyze,
        verbose=True
    )


def cmd_ingest_status(args):
    """Show ingest pipeline status."""
    from .db import init_database
    from .ingest.registrar import get_ingest_status

    init_database()
    get_ingest_status(verbose=True)


def cmd_content_list(args):
    """List content in database."""
    from .db import init_database, db_connection, get_ready_content, get_content_by_status
    from .utils.time_utils import duration_to_hms

    init_database()

    with db_connection() as conn:
        if args.status:
            content_list = get_content_by_status(conn, args.status)
        else:
            content_list = get_ready_content(conn, args.type)

    if not content_list:
        print("No content found")
        return

    print(f"\n{'ID':>4}  {'Type':10}  {'Duration':>10}  Title")
    print("-" * 70)

    for content in content_list:
        duration = duration_to_hms(content["duration_seconds"])
        print(f"{content['id']:>4}  {content['content_type']:10}  {duration:>10}  {content['title']}")

    print(f"\nTotal: {len(content_list)} items")


def cmd_content_show(args):
    """Show details for a content item."""
    from .db import init_database, db_connection, get_content_by_id, get_content_tags, get_break_points
    from .utils.time_utils import duration_to_hms

    init_database()

    with db_connection() as conn:
        content = get_content_by_id(conn, args.id)
        if not content:
            print(f"Content ID {args.id} not found")
            return

        tags = get_content_tags(conn, args.id)
        break_points = get_break_points(conn, args.id)

    print(f"\nContent ID: {content['id']}")
    print(f"Title: {content['title']}")
    print(f"Type: {content['content_type']}")
    print(f"Status: {content['status']}")
    print(f"Duration: {duration_to_hms(content['duration_seconds'])}")

    if content['series_name']:
        print(f"Series: {content['series_name']}")
    if content['season']:
        print(f"Season: {content['season']}, Episode: {content['episode']}")
    if content['year']:
        print(f"Year: {content['year']}")

    print(f"\nOriginal: {content['original_path']}")
    if content['normalized_path']:
        print(f"Normalized: {content['normalized_path']}")

    print(f"\nResolution: {content['width']}x{content['height']}")
    print(f"Aspect: {content['aspect_ratio']}")
    print(f"Codec: {content['codec']}")

    if tags:
        print(f"\nTags: {', '.join(tags)}")

    if break_points:
        print(f"\nBreak Points:")
        for bp in break_points:
            mins = int(bp['timestamp_seconds'] // 60)
            secs = int(bp['timestamp_seconds'] % 60)
            print(f"  {mins}:{secs:02d}")


def cmd_content_tag(args):
    """Add or remove tags from content."""
    from .db import init_database, db_connection, add_tag_to_content, remove_tag_from_content

    init_database()

    with db_connection() as conn:
        if args.remove:
            remove_tag_from_content(conn, args.id, args.tag)
            print(f"Removed tag '{args.tag}' from content {args.id}")
        else:
            add_tag_to_content(conn, args.id, args.tag)
            print(f"Added tag '{args.tag}' to content {args.id}")


def cmd_schedule_now(args):
    """Show what's on now."""
    from .config import load_config
    from .db import init_database
    from .schedule.engine import ScheduleEngine
    from .utils.time_utils import duration_to_hms

    init_database()
    config = load_config()
    schedule = ScheduleEngine(config)

    print("\nWhat's On Now")
    print("=" * 60)

    for channel in config.channels:
        now_playing = schedule.what_is_on(channel.number)
        if now_playing:
            elapsed = duration_to_hms(now_playing.elapsed_seconds)
            remaining = duration_to_hms(now_playing.remaining_seconds)
            print(f"\nChannel {channel.number}: {channel.name}")
            print(f"  {now_playing.entry.title}")
            print(f"  Elapsed: {elapsed}, Remaining: {remaining}")
        else:
            print(f"\nChannel {channel.number}: {channel.name}")
            print(f"  No content available")


def cmd_schedule_show(args):
    """Show schedule for a channel."""
    from .config import load_config
    from .db import init_database
    from .schedule.engine import ScheduleEngine

    init_database()
    config = load_config()
    schedule = ScheduleEngine(config)

    output = schedule.get_schedule_display(
        channel_number=args.channel,
        hours=args.hours
    )
    print(output)


def cmd_schedule_collisions(args):
    """Check for schedule collisions."""
    from .config import load_config
    from .db import init_database
    from .schedule.engine import ScheduleEngine

    init_database()
    config = load_config()
    schedule = ScheduleEngine(config)

    collisions = schedule.check_collisions()

    if collisions:
        print("\nSchedule Collisions Found:")
        print("-" * 40)
        for ch1, ch2, content in collisions:
            print(f"  '{content['title']}' on channels {ch1} and {ch2}")
    else:
        print("\nNo collisions found")


def cmd_stats(args):
    """Show database statistics."""
    from .db import init_database, db_connection, get_stats
    from .utils.time_utils import duration_to_hms

    init_database()

    with db_connection() as conn:
        stats = get_stats(conn)

    print("\nCableTV Simulator Statistics")
    print("=" * 40)

    print("\nContent by Status:")
    for status, count in sorted(stats.get("by_status", {}).items()):
        print(f"  {status:15} {count:5}")

    print("\nReady Content by Type:")
    for content_type, count in sorted(stats.get("by_type", {}).items()):
        print(f"  {content_type:15} {count:5}")

    total_hours = stats.get("total_duration_hours", 0)
    print(f"\nTotal Ready: {stats.get('total_ready', 0)} items")
    print(f"Total Duration: {total_hours:.1f} hours")

    print("\nContent by Tag:")
    tag_counts = stats.get("by_tag", {})
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {tag:15} {count:5}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="cabletv",
        description="CableTV Simulator - Authentic cable TV experience"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Start command
    start_parser = subparsers.add_parser("start", help="Start CableTV system")
    start_parser.add_argument("--windowed", "-w", action="store_true",
                              help="Start in windowed mode (not fullscreen)")
    start_parser.add_argument("--no-web", action="store_true",
                              help="Don't start web interface")
    start_parser.set_defaults(func=cmd_start)

    # Ingest commands
    ingest_parser = subparsers.add_parser("ingest", help="Ingest pipeline commands")
    ingest_sub = ingest_parser.add_subparsers(dest="ingest_command")

    # ingest scan
    scan_parser = ingest_sub.add_parser("scan", help="Scan content directories")
    scan_parser.set_defaults(func=cmd_ingest_scan)

    # ingest identify
    identify_parser = ingest_sub.add_parser("identify", help="Identify via TMDB")
    identify_parser.add_argument("--auto", "-a", action="store_true",
                                 help="Auto-accept high-confidence matches")
    identify_parser.add_argument("--skip", action="store_true",
                                 help="Skip identification stage")
    identify_parser.set_defaults(func=cmd_ingest_identify)

    # ingest transcode
    transcode_parser = ingest_sub.add_parser("transcode", help="Transcode to 640x480")
    transcode_parser.add_argument("--force", "-f", action="store_true",
                                  help="Re-transcode existing files")
    transcode_parser.add_argument("--skip", action="store_true",
                                  help="Skip transcoding (use originals)")
    transcode_parser.set_defaults(func=cmd_ingest_transcode)

    # ingest analyze
    analyze_parser = ingest_sub.add_parser("analyze", help="Analyze for break points")
    analyze_parser.add_argument("--force", "-f", action="store_true",
                                help="Re-analyze existing content")
    analyze_parser.add_argument("--skip", action="store_true",
                                help="Skip analysis stage")
    analyze_parser.set_defaults(func=cmd_ingest_analyze)

    # ingest all
    all_parser = ingest_sub.add_parser("all", help="Run full pipeline")
    all_parser.add_argument("--auto", "-a", action="store_true",
                            help="Auto-accept TMDB matches")
    all_parser.add_argument("--skip-tmdb", action="store_true",
                            help="Skip TMDB identification")
    all_parser.add_argument("--skip-transcode", action="store_true",
                            help="Skip transcoding")
    all_parser.add_argument("--skip-analyze", action="store_true",
                            help="Skip black-frame analysis")
    all_parser.set_defaults(func=cmd_ingest_all)

    # ingest status
    status_parser = ingest_sub.add_parser("status", help="Show pipeline status")
    status_parser.set_defaults(func=cmd_ingest_status)

    # Content commands
    content_parser = subparsers.add_parser("content", help="Content management")
    content_sub = content_parser.add_subparsers(dest="content_command")

    # content list
    list_parser = content_sub.add_parser("list", help="List content")
    list_parser.add_argument("--type", "-t", choices=["movie", "show", "commercial", "bumper"],
                             help="Filter by content type")
    list_parser.add_argument("--status", "-s", help="Filter by status")
    list_parser.set_defaults(func=cmd_content_list)

    # content show
    show_parser = content_sub.add_parser("show", help="Show content details")
    show_parser.add_argument("id", type=int, help="Content ID")
    show_parser.set_defaults(func=cmd_content_show)

    # content tag
    tag_parser = content_sub.add_parser("tag", help="Add/remove tag")
    tag_parser.add_argument("id", type=int, help="Content ID")
    tag_parser.add_argument("tag", help="Tag name")
    tag_parser.add_argument("--remove", "-r", action="store_true",
                            help="Remove tag instead of adding")
    tag_parser.set_defaults(func=cmd_content_tag)

    # Schedule commands
    schedule_parser = subparsers.add_parser("schedule", help="Schedule commands")
    schedule_sub = schedule_parser.add_subparsers(dest="schedule_command")

    # schedule now
    now_parser = schedule_sub.add_parser("now", help="Show what's on now")
    now_parser.set_defaults(func=cmd_schedule_now)

    # schedule show
    show_sched_parser = schedule_sub.add_parser("show", help="Show schedule")
    show_sched_parser.add_argument("--channel", "-c", type=int, help="Channel number")
    show_sched_parser.add_argument("--hours", type=int, default=3, help="Hours to show")
    show_sched_parser.set_defaults(func=cmd_schedule_show)

    # schedule check-collisions
    collision_parser = schedule_sub.add_parser("check-collisions", help="Check for collisions")
    collision_parser.set_defaults(func=cmd_schedule_collisions)

    # Stats command
    stats_parser = subparsers.add_parser("stats", help="Show statistics")
    stats_parser.set_defaults(func=cmd_stats)

    # Parse and execute
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Handle subcommand parsers
    if args.command == "ingest" and not args.ingest_command:
        ingest_parser.print_help()
        return 1
    if args.command == "content" and not args.content_command:
        content_parser.print_help()
        return 1
    if args.command == "schedule" and not args.schedule_command:
        schedule_parser.print_help()
        return 1

    if hasattr(args, "func"):
        args.func(args)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
