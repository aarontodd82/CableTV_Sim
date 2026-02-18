"""Pure-Python moon phase calculation using synodic month."""

from datetime import datetime

# Synodic month (new moon to new moon) in days
SYNODIC_MONTH = 29.53058867

# Known new moon reference: January 6, 2000 at 18:14 UTC
REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14, 0)

# Phase names and their ranges (fraction of synodic cycle)
PHASE_NAMES = [
    (0.0, 0.0625, "New Moon"),
    (0.0625, 0.1875, "Waxing Crescent"),
    (0.1875, 0.3125, "First Quarter"),
    (0.3125, 0.4375, "Waxing Gibbous"),
    (0.4375, 0.5625, "Full Moon"),
    (0.5625, 0.6875, "Waning Gibbous"),
    (0.6875, 0.8125, "Last Quarter"),
    (0.8125, 0.9375, "Waning Crescent"),
    (0.9375, 1.0001, "New Moon"),
]


def get_moon_phase(dt: datetime = None) -> dict:
    """
    Calculate the current moon phase.

    Args:
        dt: Datetime to calculate for (default: now)

    Returns:
        Dict with keys: fraction (0.0-1.0), name, illumination (0-100)
    """
    if dt is None:
        dt = datetime.now()

    # Days since reference new moon
    delta = (dt - REFERENCE_NEW_MOON).total_seconds() / 86400.0

    # Phase fraction (0.0 = new moon, 0.5 = full moon)
    fraction = (delta % SYNODIC_MONTH) / SYNODIC_MONTH

    # Phase name
    name = "New Moon"
    for low, high, phase_name in PHASE_NAMES:
        if low <= fraction < high:
            name = phase_name
            break

    # Illumination percentage (0% at new moon, 100% at full moon)
    # Uses cosine curve: 0 at fraction=0, 1 at fraction=0.5
    import math
    illumination = (1 - math.cos(2 * math.pi * fraction)) / 2 * 100

    return {
        "fraction": fraction,
        "name": name,
        "illumination": round(illumination, 1),
    }
