"""Validate public origins for native service Web entries."""
from urllib.parse import urlsplit


def origin(value: str) -> str:
    """Return a normalized absolute HTTP(S) origin or raise ``ValueError``."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid web origin") from exc
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError("invalid web origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{suffix}"
