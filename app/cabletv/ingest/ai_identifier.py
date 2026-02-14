"""Stage 2 (AI): Claude-powered content identification with TMDB tool use."""

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, update_content_status,
    update_content_metadata, add_tag_to_content, log_ingest
)
from .identifier import TMDBClient


# Valid tags that match channel config
VALID_TAGS = {
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "educational", "family", "fantasy", "gameshow", "history",
    "horror", "kids", "music", "mystery", "romance", "scifi", "thriller",
    "war", "western", "classic", "sitcom", "cult", "sports",
}

# TMDB tool definitions for Claude
TMDB_TOOLS = [
    {
        "name": "search_movie",
        "description": "Search TMDB for movies by title. Returns top results with id, title, release_date, overview, genre_ids, popularity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Movie title to search for"},
                "year": {"type": "integer", "description": "Release year (optional, helps narrow results)"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "search_tv",
        "description": "Search TMDB for TV shows by title. Returns top results with id, name, first_air_date, overview, genre_ids, popularity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "TV show title to search for"},
                "year": {"type": "integer", "description": "First air date year (optional)"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_movie",
        "description": "Get detailed info for a specific movie by TMDB ID. Returns title, release_date, genres, overview, runtime.",
        "input_schema": {
            "type": "object",
            "properties": {
                "movie_id": {"type": "integer", "description": "TMDB movie ID"},
            },
            "required": ["movie_id"],
        },
    },
    {
        "name": "get_tv",
        "description": "Get detailed info for a specific TV show by TMDB ID. Returns name, first_air_date, genres, overview, number_of_seasons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tv_id": {"type": "integer", "description": "TMDB TV show ID"},
            },
            "required": ["tv_id"],
        },
    },
    {
        "name": "get_tv_episode",
        "description": "Get details for a specific TV episode. Returns name, overview, air_date, episode_number, season_number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tv_id": {"type": "integer", "description": "TMDB TV show ID"},
                "season": {"type": "integer", "description": "Season number"},
                "episode": {"type": "integer", "description": "Episode number"},
            },
            "required": ["tv_id", "season", "episode"],
        },
    },
]

SYSTEM_PROMPT = """\
You are a media librarian identifying video files for a cable TV simulator. You receive batches of filenames grouped by directory.

Your job:
1. Examine each filename and its directory path to determine what the content is (movie or TV show).
2. Use the TMDB tools to verify your identification. For TV series, search once per series (not per episode).
3. Return a JSON array with one entry per file, in the same order as the input.

Each entry in your JSON response must have these fields:
- "index": integer, the 0-based index matching the input list
- "content_type": "movie" or "show"
- "title": display title (for shows: "Series Name S01E03", for movies: "Movie Name")
- "series_name": series name for shows, null for movies
- "season": season number for shows, null for movies
- "episode": episode number for shows, null for movies
- "year": release year (from TMDB if found, parsed from filename otherwise)
- "tmdb_id": TMDB ID if found, null otherwise
- "tags": array of exactly 2 genre tags from this valid set: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, music, mystery, romance, scifi, thriller, war, western, classic, sitcom, cult, sports. Only use 3 tags if there is a very strong reason (e.g. an animated kids comedy).
- "skip": true if this file should be skipped (not a movie/show, e.g. samples, extras), false otherwise

Guidelines:
- Parse S01E02 / 1x02 / Season 1 Episode 2 patterns from filenames
- Files numbered like "1. Show Name - Episode Title.flv" are TV episodes — use the directory name for season info
- Use directory names for context (e.g. "Breaking Bad/Season 3/" tells you the series and season)
- For movies, include the year in the title if known: "The Matrix (1999)"
- Pick the 2 most defining tags. Quality over quantity.
- Be efficient: search TMDB once per unique series/movie, not per episode
- Skip sample files, extras, behind-the-scenes, etc.
- If a file is ambiguous and TMDB search returns no results, make your best guess from the filename

IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation, no commentary. Just [ ... ]."""


def _execute_tool(client: TMDBClient, name: str, args: dict) -> str:
    """Execute a TMDB tool call and return JSON result."""
    try:
        if name == "search_movie":
            results = client.search_movie(args["title"], args.get("year"))
            # Trim to top 5, truncate overviews
            trimmed = []
            for r in results[:5]:
                trimmed.append({
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "release_date": r.get("release_date"),
                    "genre_ids": r.get("genre_ids"),
                    "popularity": r.get("popularity"),
                    "overview": (r.get("overview") or "")[:150],
                })
            return json.dumps(trimmed)

        elif name == "search_tv":
            results = client.search_tv(args["title"], args.get("year"))
            trimmed = []
            for r in results[:5]:
                trimmed.append({
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "first_air_date": r.get("first_air_date"),
                    "genre_ids": r.get("genre_ids"),
                    "popularity": r.get("popularity"),
                    "overview": (r.get("overview") or "")[:150],
                })
            return json.dumps(trimmed)

        elif name == "get_movie":
            result = client.get_movie(args["movie_id"])
            return json.dumps({
                "id": result.get("id"),
                "title": result.get("title"),
                "release_date": result.get("release_date"),
                "genres": [g["name"] for g in result.get("genres", [])],
                "runtime": result.get("runtime"),
                "overview": (result.get("overview") or "")[:200],
            })

        elif name == "get_tv":
            result = client.get_tv(args["tv_id"])
            return json.dumps({
                "id": result.get("id"),
                "name": result.get("name"),
                "first_air_date": result.get("first_air_date"),
                "genres": [g["name"] for g in result.get("genres", [])],
                "number_of_seasons": result.get("number_of_seasons"),
                "overview": (result.get("overview") or "")[:200],
            })

        elif name == "get_tv_episode":
            result = client.get_tv_episode(args["tv_id"], args["season"], args["episode"])
            return json.dumps({
                "id": result.get("id"),
                "name": result.get("name"),
                "air_date": result.get("air_date"),
                "episode_number": result.get("episode_number"),
                "season_number": result.get("season_number"),
                "overview": (result.get("overview") or "")[:200],
            })

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})


def _build_batch_message(directory: str, files: list[dict]) -> str:
    """Build the user message for a batch of files."""
    lines = [f"Directory: {directory}", "", "Files to identify:"]
    for i, f in enumerate(files):
        lines.append(f"  [{i}] {f['filename']}  (type: {f['content_type']}, duration: {f['duration_seconds']:.0f}s)")
    return "\n".join(lines)


def _extract_xml_tool_calls(text: str) -> list[dict]:
    """Extract tool calls from XML format that Claude sometimes emits as text."""
    calls = []
    for match in re.finditer(
        r'<invoke name="(\w+)">(.*?)</invoke>', text, re.DOTALL
    ):
        name = match.group(1)
        params_text = match.group(2)
        args = {}
        for param in re.finditer(
            r'<parameter name="(\w+)">([^<]+)</parameter>', params_text
        ):
            key = param.group(1)
            val = param.group(2).strip()
            # Try to parse as int
            try:
                args[key] = int(val)
            except ValueError:
                args[key] = val
        calls.append({"name": name, "args": args})
    return calls


def _parse_response(text: str) -> Optional[list[dict]]:
    """Parse JSON array from Claude's response, handling markdown fences."""
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = cleaned.index("\n")
        cleaned = cleaned[first_newline + 1:]
        # Remove closing fence
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3].rstrip()

    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in the text
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(cleaned[start:end + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _process_batch(
    anthropic_client,
    tmdb_client: Optional[TMDBClient],
    directory: str,
    files: list[dict],
    verbose: bool = True,
) -> Optional[list[dict]]:
    """Send a batch to Claude and process tool calls until we get a final response."""
    user_message = _build_batch_message(directory, files)
    tools = TMDB_TOOLS if tmdb_client else []

    messages = [{"role": "user", "content": user_message}]

    max_rounds = 10
    for round_num in range(max_rounds):
        kwargs = dict(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        response = anthropic_client.messages.create(**kwargs)

        # Check if response was truncated
        if response.stop_reason == "max_tokens":
            if verbose:
                print("    Warning: Response truncated (max_tokens)")

        # Collect text and check for tool_use in content blocks
        text_parts = []
        has_tool_use = False
        tool_results = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                has_tool_use = True
                if verbose:
                    print(f"    Tool: {block.name}({json.dumps(block.input, ensure_ascii=False)[:80]})")
                if tmdb_client:
                    result = _execute_tool(tmdb_client, block.name, block.input)
                else:
                    result = json.dumps({"error": "No TMDB client"})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        # If we have tool_use blocks, continue the loop
        if has_tool_use:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool_use blocks — we have a text response
        if text_parts:
            full_text = "\n".join(text_parts)

            # Check for XML tool calls emitted as text (model quirk)
            xml_calls = _extract_xml_tool_calls(full_text)
            if xml_calls and tmdb_client:
                if verbose:
                    for tc in xml_calls:
                        print(f"    Tool (xml): {tc['name']}({json.dumps(tc['args'], ensure_ascii=False)[:80]})")
                messages.append({"role": "assistant", "content": full_text})
                tool_results_text = []
                for tc in xml_calls:
                    result = _execute_tool(tmdb_client, tc["name"], tc["args"])
                    tool_results_text.append(f"Result for {tc['name']}: {result}")
                messages.append({"role": "user", "content": "\n".join(tool_results_text) + "\n\nNow respond with ONLY the JSON array."})
                continue

            parsed = _parse_response(full_text)
            if parsed is None and verbose:
                preview = full_text[:300].replace("\n", "\\n")
                print(f"    Parse failed. Response preview: {preview}")
            return parsed

        if verbose:
            print(f"    No text in response (stop_reason={response.stop_reason})")
        return None

    if verbose:
        print("    Warning: Hit max tool-use rounds")
    return None


def ai_identify_content(config: Config, verbose: bool = True) -> dict:
    """
    Identify scanned content using Claude AI with TMDB tool use.

    Falls back to regex+TMDB method if no Anthropic API key is configured.

    Args:
        config: Application config
        verbose: Print progress

    Returns:
        Dict with identification statistics
    """
    # Get API key from config or environment
    api_key = config.ingest.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        if verbose:
            print("No Anthropic API key configured. Falling back to regex+TMDB identification.")
        from .identifier import identify_content
        return identify_content(config, auto=True, verbose=verbose)

    # TMDB key is needed for tool calls
    tmdb_key = config.ingest.tmdb_api_key
    has_tmdb = tmdb_key and tmdb_key != "YOUR_TMDB_API_KEY_HERE"
    if not has_tmdb:
        if verbose:
            print("Warning: No TMDB API key configured. AI will identify without TMDB verification.")

    import anthropic
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    tmdb_client = TMDBClient(tmdb_key) if has_tmdb else None

    stats = {"identified": 0, "skipped": 0, "errors": 0}
    # Collect results for review summary: list of (id, filename, title, type, tags, status)
    review_log = []

    with db_connection() as conn:
        log_ingest(conn, "ai_identify", "started")

        content_list = get_content_by_status(conn, "scanned")

        if not content_list:
            if verbose:
                print("No content to identify")
            return stats

        if verbose:
            print(f"Found {len(content_list)} items to identify")

        # Skip commercials/bumpers first
        regular_content = []
        for content in content_list:
            if content["content_type"] in ("commercial", "bumper"):
                update_content_status(conn, content["id"], "identified")
                stats["skipped"] += 1
            else:
                regular_content.append(content)

        if not regular_content:
            if verbose:
                print("All items are commercials/bumpers, skipped")
            return stats

        # Group by parent directory
        dir_groups = defaultdict(list)
        for content in regular_content:
            path = content["original_path"]
            parent = str(Path(path).parent)
            dir_groups[parent].append({
                "id": content["id"],
                "filename": Path(path).name,
                "content_type": content["content_type"],
                "duration_seconds": content["duration_seconds"],
                "season": content["season"],
                "episode": content["episode"],
            })

        # Process each directory group in batches of 20
        batch_num = 0
        for directory, files in dir_groups.items():
            for batch_start in range(0, len(files), 20):
                batch = files[batch_start:batch_start + 20]
                batch_num += 1

                if verbose:
                    print(f"\nBatch {batch_num}: {directory} ({len(batch)} files)")

                try:
                    results = _process_batch(
                        anthropic_client, tmdb_client, directory, batch, verbose=verbose
                    )

                    if not results:
                        if verbose:
                            print("  Failed to get valid response from AI")
                        for f in batch:
                            stats["errors"] += 1
                            review_log.append((f["id"], f["filename"], "", "", "", "ERROR"))
                        continue

                    # Apply results to database
                    for entry in results:
                        idx = entry.get("index")
                        if idx is None or idx < 0 or idx >= len(batch):
                            continue

                        file_info = batch[idx]
                        content_id = file_info["id"]

                        if entry.get("skip", False):
                            update_content_status(conn, content_id, "identified")
                            stats["skipped"] += 1
                            review_log.append((content_id, file_info["filename"], "", "", "", "SKIP"))
                            continue

                        # Build title
                        title = entry.get("title", file_info["filename"])
                        series_name = entry.get("series_name")
                        season = entry.get("season")
                        episode = entry.get("episode")
                        year = entry.get("year")
                        tmdb_id = entry.get("tmdb_id")
                        content_type = entry.get("content_type", file_info["content_type"])

                        # Update metadata
                        update_content_metadata(
                            conn, content_id,
                            title=title,
                            series_name=series_name,
                            season=season,
                            episode=episode,
                            year=year,
                            tmdb_id=tmdb_id,
                        )

                        # Update content_type if AI changed it
                        if content_type != file_info["content_type"]:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE content SET content_type = ? WHERE id = ?",
                                (content_type, content_id)
                            )

                        # Add tags (validate against allowed set)
                        tags = entry.get("tags", [])
                        valid_tags = [t.lower().strip() for t in tags if t.lower().strip() in VALID_TAGS]
                        for tag in valid_tags:
                            add_tag_to_content(conn, content_id, tag)

                        update_content_status(conn, content_id, "identified")
                        stats["identified"] += 1

                        review_log.append((
                            content_id, file_info["filename"],
                            title, content_type,
                            ", ".join(valid_tags), "OK"
                        ))

                except Exception as e:
                    if verbose:
                        print(f"  Batch error: {e}")
                    for f in batch:
                        stats["errors"] += 1
                        review_log.append((f["id"], f["filename"], "", "", "", "ERROR"))
                    log_ingest(conn, "ai_identify", "failed", message=str(e))

        log_ingest(conn, "ai_identify", "completed",
                   message=f"Identified {stats['identified']}, skipped {stats['skipped']}, errors {stats['errors']}")

    # Print review summary
    if verbose and review_log:
        print("\n" + "=" * 90)
        print("IDENTIFICATION REVIEW")
        print("=" * 90)
        print(f"{'ID':>5}  {'Status':6}  {'Type':6}  {'Title':<40}  Tags")
        print("-" * 90)
        for content_id, filename, title, ctype, tags, status in review_log:
            display = title if title else filename
            if len(display) > 40:
                display = display[:37] + "..."
            print(f"{content_id:>5}  {status:6}  {ctype:6}  {display:<40}  {tags}")
        print("-" * 90)
        print(f"Identified: {stats['identified']}  |  Skipped: {stats['skipped']}  |  Errors: {stats['errors']}")
        print(f"\nTo fix: cabletv content edit <id> --title \"...\" --tags \"drama,action\"")
        print(f"To redo: cabletv content reset <id> [id ...] && cabletv ingest identify")

    return stats
