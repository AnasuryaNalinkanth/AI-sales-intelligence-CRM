"""Accept a meeting time only when the user's current message states it.

The model also sees earlier conversation turns, so it can copy a time from a
previous request ("tomorrow" -> the 17:45 used yesterday). The tool therefore
checks the time it receives against the times written in the current message.
"""

import re
from datetime import time

from app.core.config import settings

# "17:45", "2026-10-20T16:00", "5:45 pm", "5.45 pm", "545 pm", "5pm", "5 p.m.", "at 5",
# "noon", "midnight". Dotted and compact forms count only with AM/PM.
_TIME_MENTION = re.compile(
    r"\b(?P<hour>\d{1,2})(?:[:.]?(?P<minute>[0-5]\d))?\s*(?P<meridiem>[ap])\.?\s*m\b\.?"
    r"|(?:\b|(?<=\dT))(?P<hour24>\d{1,2}):(?P<minute24>[0-5]\d)\b"
    r"|\bat\s+(?P<hour_at>\d{1,2})\b(?!\s*(?:[:./-]|days?|hours?|hrs?|minutes?|mins?|weeks?|months?))"
    r"|\b(?P<word>noon|midday|midnight)\b",
    re.IGNORECASE,
)


def _mentions(message: str) -> list[tuple[time, ...]]:
    """Each mention as its possible times: one if explicit, two if AM/PM is unstated."""
    found: list[tuple[time, ...]] = []
    for match in _TIME_MENTION.finditer(message):
        if match["word"]:
            found.append((time(0, 0),) if match["word"].lower() == "midnight" else (time(12, 0),))
            continue
        if match["meridiem"]:
            hour, minute = int(match["hour"]), int(match["minute"] or 0)
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if match["meridiem"].lower() == "p" else 0)
            found.append((time(hour, minute),))
            continue
        raw_hour = match["hour24"] or match["hour_at"]
        hour, minute = int(raw_hour), int(match["minute24"] or 0)
        if hour > 23:
            continue
        # "17:45", "0:30" and zero-padded "05:45" are 24-hour times; "5:45" is not.
        if hour == 0 or hour > 12 or (len(raw_hour) == 2 and raw_hour.startswith("0")):
            found.append((time(hour, minute),))
        else:
            found.append((time(hour % 12, minute), time(hour % 12 + 12, minute)))
    return found


def _in_business_hours(value: time) -> bool:
    return settings.scheduling_business_start_hour <= value.hour < settings.scheduling_business_end_hour


def _label(value: time) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d}"


def ground_meeting_time(
    message: str | None,
    proposed: time | None,
) -> tuple[time | None, str | None]:
    """Return (time to use, clarification question).

    (None, None) means no time was stated in this message, so the caller asks
    for one. A question is returned only when AM/PM is genuinely ambiguous.
    """
    if message is None or proposed is None:
        return proposed, None
    for options in _mentions(message):
        if proposed not in options:
            continue
        if len(options) == 1:
            return proposed, None
        # Resolved by the configured business hours only when exactly one reading fits.
        fitting = [option for option in options if _in_business_hours(option)]
        if len(fitting) == 1:
            return fitting[0], None
        label = _label(options[0])
        return None, f"Do you mean {label} AM or {label} PM?"
    return None, None


def stated_meeting_time(message: str) -> tuple[time | None, str | None]:
    """The single time written in a message as (time, AM/PM question); (None, None) for none or several."""
    mentions = _mentions(message)
    if len(mentions) != 1:
        return None, None
    return ground_meeting_time(message, mentions[0][-1])
