"""Greedy wrapping of already-tokenized words."""


def wrap_words(words: list[str], width: int) -> list[str]:
    """Pack words into lines no longer than ``width`` when possible."""

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) < width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
