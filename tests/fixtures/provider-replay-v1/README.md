# Observed provider replays

These fixtures come from independently captured provider responses, rather than
responses constructed to match an adapter. Raw captures stay in private evidence
outside this repository and build snapshots.

The Tapas chapter structures were observed on 10 November 2026. They preserve
the tag nesting and text-node boundaries that caused two real acquisition
failures: legacy font elements and a table. Episode identifiers, chapter prose,
and identifying attribute values are replaced. Numbered text tokens let tests
check that every text node survives parsing and EPUB publication. No original
chapter text or images are included. The companion JSON records the observed
tag counts and sanitized fixture checksums.

These structural slices qualify novel markup handling. They do not establish
login, discovery, provider-wide completeness, or every reader response shape.
Synthetic malformed-input tests remain separate from the observed fixtures.

The metadata captures are from November 2026. Tapas retains a four-row JSON
episode-list envelope with sparse episode order and its declared total.
Tappytoon retains a complete captured 100-row page and 20-row continuation,
including the observed next-link boundary with a substituted cursor. Webtoon
retains a grant-list page, title metadata, and a 137-image paid reader manifest
whose service status is `STANDBY`.

All names, prose, identifiers and URLs are replaced consistently. Dates and
nonessential statistics are substituted; access/product booleans and enums,
ordering, nulls, container shapes and array lengths are retained. The companion
metadata JSON identifies the date basis, transformations, expected counts and
sanitized checksums. Original Webtoon HTTP headers were not retained with the
body captures, so transport status and session/clock scaffolding in those tests
are synthetic. The Webtoon grant fixture is one page, not a completeness proof.

Replay tests pass captured bodies through production transport and adapters.
The Webtoon acquisition replay also writes and verifies a complete ordered
archive, using generated PNGs rather than original comic images. Faults such as
changed totals, wrong identities and truncated manifests are explicitly
constructed variants of the captured fixtures. Captured reader refusals,
throttling, expired sessions, Tappytoon content delivery, and browser navigation
remain separate coverage gaps; existing synthetic tests cover those scenarios.
