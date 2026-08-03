"""Text normalization used by the slug fixture."""


def slugify(text: str) -> str:
    """Return a normalized hyphen-separated identifier."""

    return text.lower().replace(" ", "-")
