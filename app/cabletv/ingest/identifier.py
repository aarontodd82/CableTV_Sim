"""Stage 2: TMDB identification and metadata lookup."""

import re
from typing import Optional

import requests

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, update_content_status,
    update_content_metadata, add_tag_to_content, log_ingest
)


# TMDB API base URL
TMDB_API_BASE = "https://api.themoviedb.org/3"

# Genre ID to tag mapping
GENRE_MAP = {
    # Movie genres
    28: "action",
    12: "adventure",
    16: "animation",
    35: "comedy",
    80: "crime",
    99: "documentary",
    18: "drama",
    10751: "family",
    14: "fantasy",
    36: "history",
    27: "horror",
    10402: "music",
    9648: "mystery",
    10749: "romance",
    878: "scifi",
    10770: "tv-movie",
    53: "thriller",
    10752: "war",
    37: "western",
    # TV genres
    10759: "action",  # Action & Adventure
    10762: "kids",
    10763: "news",
    10764: "reality",
    10765: "scifi",   # Sci-Fi & Fantasy
    10766: "drama",   # Soap
    10767: "talk",
    10768: "war",     # War & Politics
}


class TMDBClient:
    """Client for TMDB API interactions."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def _request(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Make a request to TMDB API."""
        params = params or {}
        params["api_key"] = self.api_key

        url = f"{TMDB_API_BASE}/{endpoint}"
        response = self.session.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def search_movie(self, title: str, year: Optional[int] = None) -> list[dict]:
        """Search for movies by title."""
        params = {"query": title}
        if year:
            params["year"] = year
        result = self._request("search/movie", params)
        return result.get("results", [])

    def search_tv(self, title: str, year: Optional[int] = None) -> list[dict]:
        """Search for TV shows by title."""
        params = {"query": title}
        if year:
            params["first_air_date_year"] = year
        result = self._request("search/tv", params)
        return result.get("results", [])

    def get_movie(self, movie_id: int) -> dict:
        """Get movie details."""
        return self._request(f"movie/{movie_id}")

    def get_tv(self, tv_id: int) -> dict:
        """Get TV show details."""
        return self._request(f"tv/{tv_id}")

    def get_tv_episode(self, tv_id: int, season: int, episode: int) -> dict:
        """Get TV episode details."""
        return self._request(f"tv/{tv_id}/season/{season}/episode/{episode}")


def extract_search_title(title: str) -> str:
    """Extract clean title for TMDB search."""
    # Remove episode info
    clean = re.sub(r"\s*S\d{1,2}E\d{1,2}.*$", "", title, flags=re.IGNORECASE)
    # Remove year in parentheses
    clean = re.sub(r"\s*\(\d{4}\).*$", "", clean)
    return clean.strip()


def calculate_confidence(query: str, result: dict, content_type: str) -> float:
    """
    Calculate confidence score for a TMDB match.

    Returns score from 0.0 to 1.0
    """
    score = 0.0

    # Get the title field
    if content_type == "show":
        result_title = result.get("name", "").lower()
    else:
        result_title = result.get("title", "").lower()

    query_lower = query.lower()

    # Exact title match
    if query_lower == result_title:
        score += 0.5
    # Title contains query
    elif query_lower in result_title or result_title in query_lower:
        score += 0.3

    # Popularity boost (more popular = more likely correct)
    popularity = result.get("popularity", 0)
    if popularity > 100:
        score += 0.2
    elif popularity > 50:
        score += 0.15
    elif popularity > 10:
        score += 0.1

    # Vote count boost
    vote_count = result.get("vote_count", 0)
    if vote_count > 1000:
        score += 0.15
    elif vote_count > 100:
        score += 0.1
    elif vote_count > 10:
        score += 0.05

    # Has poster/backdrop
    if result.get("poster_path"):
        score += 0.05
    if result.get("backdrop_path"):
        score += 0.05

    return min(score, 1.0)


def identify_content(
    config: Config,
    auto: bool = False,
    confidence_threshold: float = 0.7,
    verbose: bool = True
) -> dict:
    """
    Identify scanned content using TMDB.

    Args:
        config: Application config
        auto: If True, auto-accept high-confidence matches
        confidence_threshold: Minimum confidence for auto-accept
        verbose: Print progress

    Returns:
        Dict with identification statistics
    """
    if not config.ingest.tmdb_api_key or config.ingest.tmdb_api_key == "YOUR_TMDB_API_KEY_HERE":
        if verbose:
            print("Warning: No TMDB API key configured. Skipping identification.")
            print("Get a free API key at https://www.themoviedb.org/settings/api")
        return {"identified": 0, "skipped": 0, "errors": 0, "no_api_key": True}

    client = TMDBClient(config.ingest.tmdb_api_key)
    stats = {"identified": 0, "skipped": 0, "errors": 0}

    with db_connection() as conn:
        log_ingest(conn, "identify", "started")

        # Get content needing identification
        content_list = get_content_by_status(conn, "scanned")

        if verbose:
            print(f"Found {len(content_list)} items to identify")

        for content in content_list:
            content_id = content["id"]
            title = content["title"]
            content_type = content["content_type"]

            # Skip commercials and bumpers
            if content_type in ("commercial", "bumper"):
                update_content_status(conn, content_id, "identified")
                stats["skipped"] += 1
                continue

            if verbose:
                print(f"\nIdentifying: {title} ({content_type})")

            try:
                search_title = extract_search_title(title)
                year = content["year"]

                # Search TMDB
                if content_type == "show":
                    results = client.search_tv(search_title, year)
                else:
                    results = client.search_movie(search_title, year)

                if not results:
                    if verbose:
                        print(f"  No results found")
                    # Keep as scanned, don't mark as error
                    stats["skipped"] += 1
                    continue

                # Calculate confidence for top results
                scored_results = []
                for result in results[:5]:
                    confidence = calculate_confidence(search_title, result, content_type)
                    scored_results.append((result, confidence))

                scored_results.sort(key=lambda x: x[1], reverse=True)
                best_match, best_confidence = scored_results[0]

                if verbose:
                    if content_type == "show":
                        match_title = best_match.get("name", "Unknown")
                    else:
                        match_title = best_match.get("title", "Unknown")
                    print(f"  Best match: {match_title} (confidence: {best_confidence:.2f})")

                # Decide whether to accept
                accept = False
                if auto and best_confidence >= confidence_threshold:
                    accept = True
                    if verbose:
                        print(f"  Auto-accepted (confidence >= {confidence_threshold})")
                elif not auto:
                    # Interactive mode
                    response = input(f"  Accept this match? [y/n/s(kip)]: ").strip().lower()
                    accept = response == "y"
                    if response == "s":
                        stats["skipped"] += 1
                        continue

                if accept:
                    # Update metadata
                    tmdb_id = best_match.get("id")

                    if content_type == "show":
                        new_title = best_match.get("name", title)
                        # Try to get first air date year
                        air_date = best_match.get("first_air_date", "")
                        new_year = int(air_date[:4]) if air_date else year
                    else:
                        new_title = best_match.get("title", title)
                        release_date = best_match.get("release_date", "")
                        new_year = int(release_date[:4]) if release_date else year

                    # For shows, keep the episode info
                    if content_type == "show" and content["season"] and content["episode"]:
                        display_title = f"{new_title} S{content['season']:02d}E{content['episode']:02d}"
                    else:
                        display_title = new_title

                    update_content_metadata(
                        conn, content_id,
                        title=display_title,
                        series_name=new_title if content_type == "show" else None,
                        year=new_year,
                        tmdb_id=tmdb_id,
                    )

                    # Add genre tags
                    genre_ids = best_match.get("genre_ids", [])
                    for genre_id in genre_ids:
                        if genre_id in GENRE_MAP:
                            add_tag_to_content(conn, content_id, GENRE_MAP[genre_id])

                    update_content_status(conn, content_id, "identified")
                    stats["identified"] += 1

                    if verbose:
                        print(f"  Identified as: {display_title}")
                else:
                    stats["skipped"] += 1

            except requests.RequestException as e:
                if verbose:
                    print(f"  API error: {e}")
                stats["errors"] += 1
                log_ingest(conn, "identify", "failed", content_id, str(e))
            except Exception as e:
                if verbose:
                    print(f"  Error: {e}")
                stats["errors"] += 1
                log_ingest(conn, "identify", "failed", content_id, str(e))

        log_ingest(conn, "identify", "completed",
                   message=f"Identified {stats['identified']}, skipped {stats['skipped']}")

    return stats


def skip_identification(verbose: bool = True) -> dict:
    """
    Skip identification stage and mark all scanned content as identified.

    Useful when not using TMDB or for quick testing.
    """
    stats = {"skipped": 0}

    with db_connection() as conn:
        content_list = get_content_by_status(conn, "scanned")

        for content in content_list:
            update_content_status(conn, content["id"], "identified")
            stats["skipped"] += 1

        if verbose:
            print(f"Skipped identification for {stats['skipped']} items")

    return stats
