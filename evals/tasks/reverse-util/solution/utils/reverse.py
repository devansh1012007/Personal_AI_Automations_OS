"""Reverse a sequence, preserving its type."""


def reverse(items):
    """Return ``items`` reversed; ``str`` stays ``str``, ``list`` stays ``list``.

    The input is never mutated — the reversal is always a new value,
    which is the only way an immutable input (a tuple, a string) could
    come back reversed at all.
    """
    if isinstance(items, str):
        return items[::-1]
    return type(items)(reversed(items))
