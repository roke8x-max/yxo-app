"""Single home for the serve-level row key format.

Both serve._process_row (claim keys, error_queue row_key) and
act._record_forward (forward_log row_key, used by sweep replay release_claim)
must use this one function: the rev4 e2e proved that two copies of the
formula drift apart and leave replays permanently duplicate-claimed.
Placed in core/ (not serve.py) so core/act.py can import it without a
serve<->act cycle.
"""


def serve_row_key(row) -> str:
    """Row key shared by dedup claims, error_queue rows and forward_log rows.

    任务 20 / 2.3：**键只用邮件内不可变字段** —— `row_idx` + `箱号`。

    · 去掉 customer_code 兜底：客编是业务可改字段（2026-11-16 事故就是导入把客编
      后缀从 -DMZ 改成 -XLYJN），进键等于「改个后缀就换了个身份」⇒ 旧件的认领
      键对不上，会被当成新邮件重发。
    · 空箱号用 "-" 占位（不再用 "no_key"）：避免带出 `0:no_key` 这种和客编混淆的键，
      也让「无箱号」在日志里一眼可辨。
    · row_idx 在同一封邮件内唯一，足以保证键唯一，不需要客编参与。

    兼容说明：历史 `0:CQWLJT...`（客编兜底）行不迁移 —— 切换日即 FORWARD_SINCE，
    旧件不参与新系统判定。
    """
    return "%s:%s" % (row.row_idx, (row.container_no or "").strip() or "-")
