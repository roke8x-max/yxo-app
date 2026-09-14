"""Single home for the serve-level row key format.

Both serve._process_row (claim keys, error_queue row_key) and
act._record_forward (forward_log row_key, used by sweep replay release_claim)
must use this one function: the rev4 e2e proved that two copies of the
formula drift apart and leave replays permanently duplicate-claimed.
Placed in core/ (not serve.py) so core/act.py can import it without a
serve<->act cycle.
"""


def serve_row_key(row) -> str:
    """Row key shared by dedup claims, error_queue rows and forward_log rows."""
    return "%s:%s" % (row.row_idx, row.container_no or row.customer_code or "no_key")
