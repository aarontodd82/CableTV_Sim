"""Remote schedule provider — runs local ScheduleEngine with server's seed.

Overrides position loading (from server API) and position advancing
(local cache + fire-and-forget to server). All schedule computation
runs locally for zero-latency channel switching.
"""

import requests
from typing import Optional

from ..config import Config
from .engine import ScheduleEngine


class RemoteScheduleProvider(ScheduleEngine):
    """ScheduleEngine subclass for remote mode.

    Uses the server's seed so schedule computations produce identical
    results. Positions are loaded from the server API and advances
    are sent back to the server (with local fallback).
    """

    def __init__(self, config: Config, server_url: str, seed: int):
        super().__init__(config)
        # Override the random seed with the server's seed
        self.seed = seed
        self._server_url = server_url
        self._session = requests.Session()
        self._session.timeout = 5

    def _load_positions(self) -> None:
        """Load positions from server API instead of local DB."""
        if self._positions_loaded:
            return

        try:
            resp = self._session.get(f"{self._server_url}/api/server/positions")
            resp.raise_for_status()
            data = resp.json().get("positions", {})

            # Parse "channel:group_key" -> (channel_number, group_key)
            for key_str, pos in data.items():
                parts = key_str.split(":", 1)
                if len(parts) == 2:
                    try:
                        ch = int(parts[0])
                        gk = parts[1]
                        self._positions[(ch, gk)] = pos
                    except ValueError:
                        pass

            self._positions_loaded = True
        except (requests.RequestException, ValueError) as e:
            print(f"  Warning: Could not load positions from server: {e}")
            # Fall back to loading from DB on the network share
            try:
                super()._load_positions()
            except Exception as e2:
                print(f"  Warning: DB fallback also failed: {e2}")
                self._positions_loaded = True  # Don't retry every call

    def advance_position(self, channel_number: int, group_key: str,
                         num_items: int, preserve_block_start: Optional[int] = None,
                         advance_by: int = 1) -> None:
        """Advance locally and notify server (fire-and-forget).

        Updates only the in-memory position cache (no DB write — the
        server is the source of truth). Block cache is intentionally
        NOT cleared to preserve schedule consistency (see base class
        advance_position comment).
        """
        # Update local position cache (same math as parent, skip DB write)
        key = (channel_number, group_key)
        current = self._positions.get(key, 0)
        new_pos = (current + advance_by) % num_items
        self._positions[key] = new_pos

        # NOTE: Block cache is NOT cleared here — same rationale as the
        # base ScheduleEngine.advance_position. Clearing causes walk-forward
        # cascade instability with variable-duration content.

        # Notify server (fire-and-forget)
        try:
            self._session.post(
                f"{self._server_url}/api/server/advance",
                json={
                    "channel_number": channel_number,
                    "group_key": group_key,
                    "num_items": num_items,
                    "block_start_slot": preserve_block_start or 0,
                    "advance_by": advance_by,
                },
                timeout=5,
            )
        except requests.RequestException as e:
            print(f"  Warning: Server advance notification failed: {e}")
