#*****************************
#utils.py   ver 04--
#*****************************

import datetime


def _parse_dt(dt_str: str) -> datetime.datetime:
    """Parse a timestamp string tolerant of thin/narrow spaces.
    Falls back to regular space if any thin spaces are present."""
    cleaned = dt_str.replace("\u202f", " ").replace("\u2009", " ")
    return datetime.datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")


def time_now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def duration_seconds(start: str, end: str) -> int:
    s = _parse_dt(start)
    e = _parse_dt(end)
    return int((e - s).total_seconds())

def format_hhmm(seconds):
    if seconds is None: return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02}:{m:02}"

