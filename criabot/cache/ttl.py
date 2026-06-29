import re

# Shared TTL parser used by cache objects.
#
# Supported units (kept identical to the original per-file implementations to
# avoid changing existing expiry semantics):
#   h = hours, d = days, w = weeks, m = months (30 days), y = years (365 days)
#
# NOTE: `m` intentionally means *months*, not minutes. For sub-hour expiries
# (e.g. the web-search cache) use a plain integer-seconds env var instead of
# this parser to avoid ambiguity.
_DURATION_RE = re.compile(r"^(\d+)([hdwmy])$")

_UNIT_SECONDS = {
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "m": 30 * 24 * 60 * 60,
    "y": 365 * 24 * 60 * 60,
}


def parse_time_to_seconds(time_str: str, default: int = 3600) -> int:
    """Parse a duration string like ``"1h"``, ``"4h"``, ``"7d"`` into seconds.

    Falls back to ``default`` (1 hour) when the value is empty or malformed.
    """

    if not time_str:
        return default

    match = _DURATION_RE.match(time_str.strip().lower())
    if not match:
        return default

    value, unit = match.groups()
    return int(value) * _UNIT_SECONDS[unit]
