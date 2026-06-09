"""Identity signal capture — observed actor pairs from ingest payloads.

Each ingest script feeds `record_signal()` whenever it sees two identity
handles in the same payload (e.g. jira gives `accountId + emailAddress +
displayName` together for issue creator/assignee/commenter). Pairs are
stored in `events.db::identity_signals` with an `n_obs` counter.

The reconcile pass (`derive/identity_reconcile.py`) reads this table and
back-fills missing fields onto `config/people.yaml` entries.

Design notes
------------
- Generic. No source-specific schema beyond the `source` label.
- Eventual. Fills entries as data arrives; no batch.
- Symmetric. (a → b) and (b → a) collapse to one row via canonical
  ordering on (type, value).
- Confidence-aware. Repeated sightings increment `n_obs`, so the
  reconciler can prefer high-confidence values when multiple candidates
  exist for the same missing field.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_signals (
    observed_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    key_a_type    TEXT NOT NULL,
    key_a_value   TEXT NOT NULL,
    key_b_type    TEXT NOT NULL,
    key_b_value   TEXT NOT NULL,
    n_obs         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (key_a_type, key_a_value, key_b_type, key_b_value)
);

CREATE INDEX IF NOT EXISTS idx_signals_a
    ON identity_signals(key_a_type, key_a_value);
CREATE INDEX IF NOT EXISTS idx_signals_b
    ON identity_signals(key_b_type, key_b_value);
"""

# Accepted key types — match people.yaml field names where possible.
VALID_TYPES = {
    "email", "jira_id", "slack_id", "slack_handle",
    "github", "git_name", "name",
}


def init(conn: sqlite3.Connection) -> None:
    """Create signal table + indices (idempotent)."""
    conn.executescript(SCHEMA)
    conn.commit()


def _normalize(typ: str, val: str) -> str:
    val = val.strip()
    if typ == "email":
        return val.lower()
    return val


def _ordered(a_type: str, a_val: str, b_type: str, b_val: str
             ) -> tuple[str, str, str, str]:
    """Canonical ordering on (type, value) so (a,b) and (b,a) collapse."""
    if (a_type, a_val) > (b_type, b_val):
        return b_type, b_val, a_type, a_val
    return a_type, a_val, b_type, b_val


def record_signal(
    conn: sqlite3.Connection,
    source: str,
    a_type: str, a_value,
    b_type: str, b_value,
) -> None:
    """Upsert a single {key_a ↔ key_b} pair signal.

    No-ops on missing values, identical pairs, or unrecognised types.
    Caller is responsible for `conn.commit()`.
    """
    if not a_value or not b_value:
        return
    if a_type not in VALID_TYPES or b_type not in VALID_TYPES:
        return
    a_val = _normalize(a_type, str(a_value))
    b_val = _normalize(b_type, str(b_value))
    if a_type == b_type and a_val == b_val:
        return
    at, av, bt, bv = _ordered(a_type, a_val, b_type, b_val)
    now = (datetime.now(timezone.utc)
           .isoformat(timespec="seconds").replace("+00:00", "Z"))
    conn.execute(
        """
        INSERT INTO identity_signals
            (observed_at, source, key_a_type, key_a_value,
             key_b_type, key_b_value, n_obs)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(key_a_type, key_a_value, key_b_type, key_b_value)
        DO UPDATE SET n_obs = n_obs + 1, observed_at = excluded.observed_at
        """,
        (now, source, at, av, bt, bv),
    )


def record_user_dict(
    conn: sqlite3.Connection,
    source: str,
    user: dict | None,
) -> None:
    """Convenience: record all pairs implied by a single user-dict payload.

    Accepts the common Atlassian user shape:
        {accountId, emailAddress, displayName, ...}
    plus Slack-shaped:
        {id, profile.email, profile.real_name, name}
    plus Github-shaped:
        {login, email, name}

    Extracts whatever fields are present and records all pairwise
    combinations. Unknown keys are ignored.
    """
    if not user:
        return

    # Pull supported fields, mapped to canonical key types.
    fields: dict[str, str] = {}

    # Atlassian
    if (v := user.get("accountId")):
        fields["jira_id"] = v
    if (v := user.get("emailAddress")):
        fields["email"] = v
    if (v := user.get("displayName")):
        fields["name"] = v
    # Slack
    if (v := user.get("id")) and str(v).startswith(("U", "W")):
        fields["slack_id"] = v
    if isinstance(user.get("profile"), dict):
        p = user["profile"]
        if (v := p.get("email")):
            fields.setdefault("email", v)
        if (v := p.get("real_name")) or (v := p.get("display_name")):
            fields.setdefault("name", v)
    if (v := user.get("name")) and "slack_id" in fields:
        fields["slack_handle"] = v
    # Github
    if (v := user.get("login")):
        fields["github"] = v
    if "email" not in fields and (v := user.get("email")):
        fields["email"] = v
    if "name" not in fields and isinstance(user.get("name"), str):
        fields["name"] = user["name"]

    # Emit all pairs.
    items = list(fields.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            record_signal(conn, source, items[i][0], items[i][1],
                          items[j][0], items[j][1])


def record_pairs(
    conn: sqlite3.Connection,
    source: str,
    pairs: Iterable[tuple[str, str, str, str]],
) -> None:
    """Batch helper: pairs = [(a_type, a_val, b_type, b_val), ...]."""
    for p in pairs:
        record_signal(conn, source, *p)
