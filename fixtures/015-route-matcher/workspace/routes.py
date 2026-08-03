"""Matching of simple slash-delimited route patterns."""

from urllib.parse import unquote


def match_route(pattern: str, path: str) -> dict[str, str] | None:
    """Match a route after trimming outer slashes and URL-decoding the path."""

    pattern_text = pattern.strip("/")
    path_text = unquote(path).strip("/")
    pattern_parts = pattern_text.split("/") if pattern_text else []
    path_parts = path_text.split("/") if path_text else []
    if len(pattern_parts) != len(path_parts):
        return None

    parameters: dict[str, str] = {}
    for token, value in zip(pattern_parts, path_parts, strict=True):
        if token.startswith(":"):
            parameters[token[1:]] = value
        elif token != value:
            return None
    return parameters
