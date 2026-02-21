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
    update_content_metadata, add_tag_to_content, get_content_tags,
    get_all_series_tags, log_ingest, remove_tag_from_content,
    clear_content_tags
)
from .identifier import TMDBClient


# Valid tags that match channel config
VALID_TAGS = {
    "action", "adventure", "animation", "comedy", "crime", "documentary",
    "drama", "educational", "family", "fantasy", "gameshow", "history",
    "horror", "kids", "music", "mystery", "romance", "scifi", "thriller",
    "western", "classic", "disney", "sitcom", "sports",
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

MUSIC_SYSTEM_PROMPT = """\
You are a music librarian identifying music video files for a cable TV simulator. You receive batches of filenames.

Your job:
1. Examine each filename to extract the artist name, song title, and year.
2. Standardize artist names (correct capitalization, full names).
3. Return a JSON array with one entry per file, in the same order as the input.

Each entry must have these fields:
- "index": integer, the 0-based index matching the input list
- "artist": artist/band name (properly capitalized)
- "title": song title (just the song name, not "Artist - Title")
- "year": release year if present in filename, null if unknown
- "content_type": always "music"
- "tags": always ["music"]
- "skip": true if this file is not a music video (e.g. playlist files, artwork), false otherwise

Expected filename format: "Artist - Title (Year).mp4" but handle variations.

IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation, no commentary. Just [ ... ]."""

SYSTEM_PROMPT = """\
You are a media librarian identifying video files for a cable TV simulator. You receive batches of filenames grouped by directory.

Your job:
1. Examine each filename and its directory path to determine what the content is.
2. Use the TMDB tools to verify your identification. For TV series, search once per series (not per episode).
3. Return a JSON array with one entry per file, in the same order as the input.

Each entry in your JSON response must have these fields:
- "index": integer, the 0-based index matching the input list
- "content_type": "movie", "show", "commercial", or "bumper"
- "title": display title (shows: "Series Name S01E03", movies: "Movie Name (Year)")
- "series_name": series name for shows, null for movies/commercials/bumpers
- "season": season number for shows, null otherwise
- "episode": episode number for shows, null otherwise
- "year": release year (from TMDB if found, parsed from filename otherwise)
- "tmdb_id": TMDB ID if found, null otherwise
- "tags": array of genre tags (see TAGGING RULES below). For commercials/bumpers: empty array [].
- "skip": true ONLY for junk files (samples, extras, behind-the-scenes, .nfo, artwork). NOT for commercials/bumpers — classify those properly instead.

VALID TAGS: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, mystery, romance, scifi, sitcom, sports, thriller, western
BONUS TAGS (added ON TOP of the 2 base tags): classic (pre-1980 content), disney (Disney/Pixar productions), music (music content only)

CONTENT TYPE RULES:
- "movie": Feature films, TV movies, standalone specials
- "show": TV series episodes (identified by S01E01, 1x01, season/episode numbering, or TMDB)
- "commercial": Short clips (typically < 120s) that are advertisements, promos, station IDs, network idents
- "bumper": Very short clips (typically < 30s) — channel bumpers, "we'll be right back" clips, network transitions
- If the provided type says "commercial" or "bumper" but it's clearly a full TV episode or movie, OVERRIDE it to the correct type

CRITICAL TAGGING RULES:
1. Every movie and show MUST have exactly 2 base tags. Pick the 2 most defining genres. No more, no fewer (before bonus tags).
2. ANIMATION: Any animated content (cartoons, anime, CGI animation) MUST include "animation" as one of its 2 base tags. Examples: The Simpsons = animation, comedy. Batman: The Animated Series = animation, action. Toy Story = animation, family + disney bonus.
3. SITCOM: Live-action situation comedies MUST use "sitcom" as one of their 2 base tags. Examples: Seinfeld = sitcom, comedy. Friends = sitcom, comedy. The Fresh Prince = sitcom, comedy + family.
4. KIDS: Content specifically made for children (Sesame Street, Barney, Teletubbies, Nickelodeon/PBS Kids shows) MUST include "kids" as one tag. Can combine: Rugrats = animation, kids.
5. CLASSIC bonus: Pre-1980 content gets "classic" as a bonus tag (3 total). Post-1980 content NEVER gets "classic".
6. DISNEY bonus: Walt Disney Pictures, Walt Disney Animation, Pixar, Disney Channel originals get "disney" as a bonus tag.
7. MUSIC tag: ONLY for music video content. NEVER use "music" for movies/shows about musicians or with soundtracks.
8. Do NOT duplicate tags. "animation, animation" is wrong.

EXAMPLES:
- The Simpsons S03E05 → content_type: "show", tags: ["animation", "comedy"]
- Seinfeld S04E11 → content_type: "show", tags: ["sitcom", "comedy"]
- Rugrats S02E01 → content_type: "show", tags: ["animation", "kids"]
- The X-Files S01E01 → content_type: "show", tags: ["scifi", "mystery"]
- Law & Order S05E10 → content_type: "show", tags: ["crime", "drama"]
- Toy Story (1995) → content_type: "movie", tags: ["animation", "family", "disney"]
- Casablanca (1942) → content_type: "movie", tags: ["drama", "romance", "classic"]
- Coca-Cola ad (30s clip) → content_type: "commercial", tags: []
- NBC bumper (5s clip) → content_type: "bumper", tags: []
- LA Confidential (1997) → content_type: "movie", tags: ["crime", "thriller"] (NOT a commercial despite "confidential" in name)

GUIDELINES:
- Parse S01E02 / 1x02 / Season 1 Episode 2 patterns from filenames
- Files numbered like "1. Show Name - Episode Title.flv" are TV episodes — use the directory name for season info
- Use directory names for context (e.g. "Breaking Bad/Season 3/" tells you the series and season)
- Be efficient: search TMDB once per unique series/movie, not per episode
- If a file is ambiguous and TMDB search returns no results, make your best guess from the filename

IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation, no commentary. Just [ ... ]."""

REVIEW_SYSTEM_PROMPT = """\
You are a media librarian reviewing content that was flagged for tagging issues in a cable TV simulator.

Each item has already been identified (title, type, year are known). Your job is ONLY to assign correct tags.

VALID TAGS: action, adventure, animation, comedy, crime, documentary, drama, educational, family, fantasy, gameshow, history, horror, kids, mystery, romance, scifi, sitcom, sports, thriller, western
BONUS TAGS (added ON TOP of 2 base tags): classic (pre-1980), disney (Disney/Pixar)

RULES:
1. Every movie and show MUST have exactly 2 base tags (before bonus tags).
2. ANIMATION: Cartoons, anime, CGI → MUST include "animation". Simpsons = animation, comedy. Toy Story = animation, family.
3. SITCOM: Live-action sitcoms → MUST include "sitcom". Seinfeld = sitcom, comedy. Friends = sitcom, comedy.
4. KIDS: Children's shows (Sesame Street, Barney, Nickelodeon, PBS Kids) → MUST include "kids". Rugrats = animation, kids.
5. Pre-1980 content gets "classic" bonus. Post-1980 NEVER gets "classic".
6. Disney/Pixar gets "disney" bonus.
7. "music" is ONLY for music videos. Never for movies/shows about musicians.

You will receive a list of items with their current tags and the reason they were flagged.
Return a JSON array with one entry per item:
- "id": the content ID
- "tags": corrected array of tags (2 base + any applicable bonus tags)

IMPORTANT: Respond with ONLY the JSON array. No markdown, no explanation. Just [ ... ]."""


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


def _build_batch_message(directory: str, files: list[dict], series_tags: Optional[dict[str, list[str]]] = None) -> str:
    """Build the user message for a batch of files.

    Args:
        directory: Parent directory path
        files: List of file info dicts
        series_tags: Optional mapping of series_name -> existing tags for consistency
    """
    lines = [f"Directory: {directory}", ""]

    # If we know existing tags for series in this directory, tell the AI
    if series_tags:
        matched = _match_series_context(directory, series_tags)
        for name, tags in matched.items():
            lines.append(f'NOTE: Existing episodes of "{name}" are tagged: {", ".join(tags)}')
            lines.append("Use these same tags unless there is a very strong reason not to.")
            lines.append("")

    lines.append("Files to identify:")
    for i, f in enumerate(files):
        lines.append(f"  [{i}] {f['filename']}  (type: {f['content_type']}, duration: {f['duration_seconds']:.0f}s)")
    return "\n".join(lines)


def _match_series_context(directory: str, series_tags: dict[str, list[str]]) -> dict[str, list[str]]:
    """Match directory path components against known series names.

    Checks if any known series name appears in the directory path
    (case-insensitive). Returns matching series -> tags.
    """
    dir_lower = directory.lower()
    matched = {}
    for name, tags in series_tags.items():
        if name.lower() in dir_lower:
            matched[name] = tags
    return matched


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
    series_tags: Optional[dict[str, list[str]]] = None,
) -> Optional[list[dict]]:
    """Send a batch to Claude and process tool calls until we get a final response."""
    # Detect music batches — use music-specific prompt with no TMDB tools
    is_music = files and files[0].get("content_type") == "music"
    system_prompt = MUSIC_SYSTEM_PROMPT if is_music else SYSTEM_PROMPT

    user_message = _build_batch_message(directory, files, series_tags=series_tags)
    tools = [] if is_music else (TMDB_TOOLS if tmdb_client else [])

    messages = [{"role": "user", "content": user_message}]

    max_rounds = 10
    for round_num in range(max_rounds):
        kwargs = dict(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            system=system_prompt,
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


# Tags that count as "base" tags (not bonus tags)
_BONUS_TAGS = {"classic", "disney"}
_BASE_TAGS = VALID_TAGS - _BONUS_TAGS - {"music"}


def _validate_and_fix_tags(conn, config: Config, verbose: bool = True) -> list[dict]:
    """Post-identification validation: enforce tagging rules in code.

    Runs after all AI batches complete. Auto-fixes what it can,
    returns a list of flagged items that need AI review.

    Returns:
        List of dicts: [{"id": int, "title": str, "content_type": str,
                         "year": int|None, "tags": [str], "reason": str}]
    """
    flagged = []
    auto_fixed = 0

    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, content_type, year, series_name
        FROM content WHERE status IN ('identified', 'ready')
        AND content_type IN ('movie', 'show')
    """)
    items = cursor.fetchall()

    for item in items:
        cid = item["id"]
        tags = get_content_tags(conn, cid)
        year = item["year"]
        ctype = item["content_type"]
        changed = False

        # Rule: Remove "music" tag from non-music content
        if "music" in tags and ctype != "music":
            remove_tag_from_content(conn, cid, "music")
            tags = [t for t in tags if t != "music"]
            changed = True

        # Rule: Pre-1980 movies missing "classic" → auto-add
        if ctype == "movie" and year and year < 1980 and "classic" not in tags:
            add_tag_to_content(conn, cid, "classic")
            tags.append("classic")
            changed = True

        # Rule: Post-1980 content has "classic" → auto-remove
        if year and year >= 1980 and "classic" in tags:
            remove_tag_from_content(conn, cid, "classic")
            tags = [t for t in tags if t != "classic"]
            changed = True

        if changed:
            auto_fixed += 1

        # Count base tags (excluding bonus tags)
        base_tags = [t for t in tags if t in _BASE_TAGS]

        # Flag: fewer than 2 base tags → needs AI review
        if len(base_tags) < 2:
            flagged.append({
                "id": cid,
                "title": item["title"],
                "content_type": ctype,
                "year": year,
                "series_name": item["series_name"],
                "tags": tags,
                "reason": f"Only {len(base_tags)} base tag(s): {', '.join(base_tags) or '(none)'}",
            })

    # Reachability check (informational warning)
    if verbose and config.channels:
        # Build map of which tag+type combos reach a channel
        reachable_tags = set()
        for ch in config.channels:
            for tag in ch.tags:
                for ct in ch.content_types:
                    reachable_tags.add((tag, ct))

        cursor.execute("""
            SELECT c.id, c.title, c.content_type FROM content c
            WHERE c.status IN ('identified', 'ready')
            AND c.content_type IN ('movie', 'show')
        """)
        unreachable_count = 0
        for row in cursor.fetchall():
            row_tags = get_content_tags(conn, row["id"])
            ct = row["content_type"]
            if not any((t, ct) in reachable_tags for t in row_tags):
                unreachable_count += 1

        if unreachable_count > 0:
            print(f"\n  Warning: {unreachable_count} items have tags+type that don't reach any channel")

    if verbose:
        if auto_fixed:
            print(f"\n  Validation auto-fixed {auto_fixed} items (classic/music tag rules)")
        if flagged:
            print(f"  Validation flagged {len(flagged)} items for AI review (too few base tags)")

    return flagged


def _ai_review_flagged(
    anthropic_client,
    flagged: list[dict],
    conn,
    verbose: bool = True,
) -> int:
    """Send flagged items to Claude for tag correction.

    Args:
        anthropic_client: Anthropic API client
        flagged: List of flagged item dicts from _validate_and_fix_tags
        conn: Database connection
        verbose: Print progress

    Returns:
        Number of items corrected
    """
    if not flagged:
        return 0

    # Build message listing all flagged items
    lines = ["Items that need corrected tags:", ""]
    for i, item in enumerate(flagged):
        year_str = f" ({item['year']})" if item.get("year") else ""
        series_str = f" [series: {item['series_name']}]" if item.get("series_name") else ""
        lines.append(
            f"  [{i}] id={item['id']}  {item['content_type']}  "
            f"\"{item['title']}\"{year_str}{series_str}  "
            f"current tags: [{', '.join(item['tags'])}]  "
            f"reason: {item['reason']}"
        )

    user_message = "\n".join(lines)

    if verbose:
        print(f"\n  Sending {len(flagged)} flagged items for AI review...")

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = "\n".join(b.text for b in response.content if b.type == "text")
        results = _parse_response(text)

        if not results:
            if verbose:
                print("  AI review: failed to parse response")
            return 0

        corrected = 0
        for entry in results:
            item_id = entry.get("id")
            new_tags = entry.get("tags", [])

            # Validate tags
            valid_new = [t.lower().strip() for t in new_tags if t.lower().strip() in VALID_TAGS]
            base_new = [t for t in valid_new if t in _BASE_TAGS]

            if len(base_new) < 2:
                continue  # AI didn't fix it properly, skip

            # Find the flagged item
            item = next((f for f in flagged if f["id"] == item_id), None)
            if not item:
                continue

            # Replace all tags
            clear_content_tags(conn, item_id)
            for tag in valid_new:
                add_tag_to_content(conn, item_id, tag)

            corrected += 1
            if verbose:
                print(f"    Fixed id={item_id}: {', '.join(item['tags'])} → {', '.join(valid_new)}")

        if verbose:
            print(f"  AI review corrected {corrected}/{len(flagged)} items")

        return corrected

    except Exception as e:
        if verbose:
            print(f"  AI review error: {e}")
        return 0


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

        # Separate music; auto-skip only confirmed commercials from commercials/ dir
        regular_content = []
        music_content = []
        for content in content_list:
            orig = (content["original_path"] or "").lower().replace("\\", "/")
            is_from_commercials_dir = "commercials/originals/" in orig or "commercials\\originals\\" in content["original_path"].lower()
            is_short = content["duration_seconds"] < 120

            if content["content_type"] == "commercial" and is_from_commercials_dir and is_short:
                # Confirmed commercial from commercials directory — skip AI
                update_content_status(conn, content["id"], "identified")
                stats["skipped"] += 1
            elif content["content_type"] == "music":
                music_content.append(content)
            else:
                # Everything else goes to AI (including mistyped commercials/bumpers)
                regular_content.append(content)

        # Merge music back into regular for AI processing (they get a different prompt)
        regular_content.extend(music_content)

        if not regular_content:
            if verbose:
                print("All items are confirmed commercials, skipped")
            return stats

        # Load existing series tags for consistency (preventative)
        series_tags = get_all_series_tags(conn)
        if verbose and series_tags:
            print(f"Loaded tag context for {len(series_tags)} existing series")

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
                        anthropic_client, tmdb_client, directory, batch, verbose=verbose,
                        series_tags=series_tags
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
                        artist = entry.get("artist")
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
                            artist=artist,
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

                        # Incremental series_tags update: first batch wins
                        if series_name and valid_tags and series_name not in series_tags:
                            series_tags[series_name] = valid_tags

                except Exception as e:
                    if verbose:
                        print(f"  Batch error: {e}")
                    for f in batch:
                        stats["errors"] += 1
                        review_log.append((f["id"], f["filename"], "", "", "", "ERROR"))
                    log_ingest(conn, "ai_identify", "failed", message=str(e))

        # Post-identification validation and AI review
        if stats["identified"] > 0:
            if verbose:
                print("\nRunning post-identification validation...")
            flagged = _validate_and_fix_tags(conn, config, verbose=verbose)
            if flagged:
                corrected = _ai_review_flagged(
                    anthropic_client, flagged, conn, verbose=verbose
                )
                stats["validated"] = corrected

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


def check_tag_consistency(verbose: bool = True) -> list[dict]:
    """Check that all episodes of each series have consistent tags.

    Returns:
        List of dicts with inconsistency info:
        [{"series": str, "total": int, "tag_sets": [(tags, count), ...], "suggestion": [str]}]
    """
    from ..db import db_connection, get_content_tags

    inconsistencies = []

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT series_name FROM content
            WHERE series_name IS NOT NULL AND status IN ('identified', 'ready')
            ORDER BY series_name
        """)
        series_names = [row["series_name"] for row in cursor.fetchall()]

        for name in series_names:
            cursor.execute("""
                SELECT id FROM content
                WHERE series_name = ? AND status IN ('identified', 'ready')
            """, (name,))
            episodes = cursor.fetchall()

            tag_set_counts: dict[tuple, list[int]] = {}
            for ep in episodes:
                tags = tuple(sorted(get_content_tags(conn, ep["id"])))
                if tags not in tag_set_counts:
                    tag_set_counts[tags] = []
                tag_set_counts[tags].append(ep["id"])

            if len(tag_set_counts) <= 1:
                continue

            # Sort by count descending
            sorted_sets = sorted(tag_set_counts.items(), key=lambda x: len(x[1]), reverse=True)
            most_common = list(sorted_sets[0][0])

            inconsistencies.append({
                "series": name,
                "total": len(episodes),
                "tag_sets": [(list(tags), ids) for tags, ids in sorted_sets],
                "suggestion": most_common,
            })

    if verbose:
        if not inconsistencies:
            print("\nTAG CONSISTENCY: All series have consistent tags.")
        else:
            print("\n" + "=" * 70)
            print("TAG CONSISTENCY CHECK")
            print("=" * 70)
            for info in inconsistencies:
                ep_word = "episode" if info["total"] == 1 else "episodes"
                print(f"\nWARNING: {info['series']} ({info['total']} {ep_word}) has inconsistent tags:")
                for tags, ids in info["tag_sets"]:
                    n = len(ids)
                    ep_w = "episode" if n == 1 else "episodes"
                    print(f"  {n:>4} {ep_w}: {', '.join(tags) if tags else '(none)'}")
                suggestion = ", ".join(info["suggestion"]) if info["suggestion"] else "(none)"
                print(f"  Suggestion: apply most common tags ({suggestion}) to all")
                id_list = " ".join(
                    str(i) for tags, ids in info["tag_sets"][1:]
                    for i in ids
                )
                if id_list:
                    print(f"  Fix: cabletv content edit {id_list} --tags \"{suggestion}\"")
            print("=" * 70)

    return inconsistencies
