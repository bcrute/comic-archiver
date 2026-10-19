"""Bounded, metadata-only discovery through links in the account's reading list.

This report is an access observation, never an authorization source for imports.
Every archive still checks the live episode info before requesting its reader.
"""


CATEGORIES = ("subscribed", "recent", "updated")


def observed_id(value):
    return isinstance(value, str) and 0 < len(value) <= 20 and value.isascii() and value.isdigit() and 0 < int(value)


