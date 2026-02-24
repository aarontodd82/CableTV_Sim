"""Server-side schedule manager with consumed-slot tracking.

Wraps ScheduleEngine to ensure each content block's position is only
advanced once, even if multiple clients (or re-tunes) trigger it.
"""

import threading
from typing import Optional

from .engine import ScheduleEngine
from ..utils.time_utils import get_slot_number


class ServerScheduleManager:
    """Wraps ScheduleEngine with consumed-slot tracking for server mode.

    Tracks which (channel, block_start_slot) pairs have already been
    advanced so the same episode position isn't incremented multiple times
    when multiple remote clients or re-tunes hit the same content block.
    """

    def __init__(self, schedule_engine: ScheduleEngine):
        self.engine = schedule_engine
        # Save unbound reference to the original advance_position method
        # so it won't be affected by monkey-patching on the engine instance
        self._original_advance = ScheduleEngine.advance_position
        self._consumed: dict[tuple[int, int], int] = {}  # (channel, block_start_slot) -> content_id
        self._lock = threading.Lock()

    @property
    def seed(self) -> int:
        return self.engine.seed

    def try_advance(self, channel_number: int, group_key: str, num_items: int,
                    block_start_slot: int, advance_by: int = 1,
                    content_id: int = 0) -> bool:
        """Advance episode position if this block hasn't been consumed yet.

        Args:
            channel_number: Channel number
            group_key: Series name or standalone key
            num_items: Total items in the group
            block_start_slot: Slot number where this content block starts
            advance_by: Number of positions to advance
            content_id: Content ID for tracking (informational)

        Returns:
            True if position was advanced, False if already consumed
        """
        with self._lock:
            key = (channel_number, block_start_slot)
            if key in self._consumed:
                return False
            self._consumed[key] = content_id
            self._prune_consumed()

        self._original_advance(
            self.engine, channel_number, group_key, num_items,
            preserve_block_start=block_start_slot,
            advance_by=advance_by,
        )
        return True

    def _prune_consumed(self) -> None:
        """Remove consumed-slot entries older than 6 hours.

        Must be called with self._lock held. Keeps the dict bounded
        during long-running server sessions.
        """
        from datetime import datetime
        now = datetime.now()
        cutoff_slot = get_slot_number(
            now, self.engine.epoch, self.engine.slot_duration
        ) - (6 * 60 // self.engine.slot_duration)
        self._consumed = {
            k: v for k, v in self._consumed.items()
            if k[1] >= cutoff_slot
        }

    def get_all_positions(self) -> dict[str, int]:
        """Get all series positions as a flat dict for API response.

        Returns:
            Dict mapping "channel:group_key" -> position
        """
        self.engine._load_positions()
        return {
            f"{ch}:{gk}": pos
            for (ch, gk), pos in self.engine._positions.items()
        }
