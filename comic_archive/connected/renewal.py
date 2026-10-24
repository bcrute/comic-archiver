"""Fixed rotation metadata, never a peer, consent or read-authority factory."""

from .jsoncodec import require
from .signaling import _bundle
from .wire import _id

DAY_MS = 86400000
LIFETIME_MS = 30 * DAY_MS
WINDOW_MS = 7 * DAY_MS
MIN_EXTENSION_MS = 22 * DAY_MS
FIELDS = {
    "pairing_id", "account_binding", "reader_keys", "approval_id", "shares",
    "from_generation", "generation", "previous_expires_at", "token_expires_at",
}


def validate(value, row=None, *, completed=False):
    require(isinstance(value, dict) and set(value) == FIELDS)
    for name in ("pairing_id", "account_binding", "approval_id"):
        _id(value[name])
    _bundle(value["reader_keys"])
    for name in ("from_generation", "generation"):
        require(type(value[name]) is int and 0 <= value[name] < 2**31)
    require(value["generation"] == value["from_generation"] + 1)
    for name in ("previous_expires_at", "token_expires_at"):
        require(type(value[name]) is int and 0 <= value[name] <= 2**53 - 1)
    require(value["token_expires_at"] - value["previous_expires_at"] >= MIN_EXTENSION_MS)
    require(isinstance(value["shares"], list) and 1 <= len(value["shares"]) <= 16)
    for share in value["shares"]:
        _id(share)
    require(len(set(value["shares"])) == len(value["shares"]))
    if row is not None:
        require(row["status"] == "approved")
        for name in ("pairing_id", "account_binding", "reader_keys", "approval_id"):
            require(value[name] == row[name])
        baseline = value["generation"] if completed else value["from_generation"]
        require(
            (row["generation"] == baseline and row["shares"] == value["shares"])
            or (row["generation"] > value["generation"]
                and row["shares"] == [share for share in value["shares"] if share in row["shares"]])
        )
