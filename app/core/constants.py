import os
import re


def parse_time_to_seconds(time_str: str) -> int:
    """
    Parse time string with units to seconds.
    Supported units: h (hours), d (days), w (weeks), m (months), y (years)
    Example: '1d' -> 86400, '2h' -> 7200, '1w' -> 604800
    """
    if not time_str:
        return 3600  # Default to 1 hour

    # Match pattern: number followed by unit letter
    match = re.match(r'^(\d+)([hdwmy])$', time_str.lower())
    if not match:
        raise ValueError(f"Invalid time format: {time_str}. Expected format: number + unit (h/d/w/m/y)")

    value, unit = match.groups()
    value = int(value)

    # Convert to seconds
    if unit == 'h':  # hours
        return value * 60 * 60
    elif unit == 'd':  # days
        return value * 24 * 60 * 60
    elif unit == 'w':  # weeks
        return value * 7 * 24 * 60 * 60
    elif unit == 'm':  # months (assuming 30 days)
        return value * 30 * 24 * 60 * 60
    elif unit == 'y':  # years (assuming 365 days)
        return value * 365 * 24 * 60 * 60
    else:
        raise ValueError(f"Unsupported time unit: {unit}")


# Chat Configuration
CHAT_EXPIRE_TIME: int = parse_time_to_seconds(os.environ.get("CHAT_EXPIRE_TIME", "1h"))
