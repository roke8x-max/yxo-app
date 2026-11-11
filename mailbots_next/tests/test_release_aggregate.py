# -*- coding: utf-8 -*-
"""放箱令 / 还箱令 —— 聚合与合并发送（任务 11 第三阶段）。

不连 IMAP、不发真信、不读真实库：
收件人 / 负责人 / 发件账号 / IMAP 取附件 / send_smtp / mark_seen / notifier 全部打桩，
池表用 conftest 指到临时 dedup.db，每个用例前后清空。

桩一律打在 `mailbots_next.core.aggregate` 的命名空间上：
aggregate.py 是 `from .store import get_recipients` 这种写法，函数对象在**导入那一刻**
就绑进了 aggregate 的命名空间，改定义方对 aggregate 里的引用无效
（任务 10 踩过同一个坑）。唯一例外见 `forward_writes` fixture 的注释。
"""
import io
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from mailbots_next.core import aggregate

RELEASE_NOTE = ("请填写放箱令邮件后面的数字编码在放箱令上面提箱，"
                "具体车队再盖鲜章提柜。有效期10月30日到11月1日")
RELEASE_VALID = "10月30日到11月1日"


# ---------------------------------------------------------------- 工具

class _FakeNotifier:
    def __init__(self):
        self.config_missing = []
        self.program_error = []

    def send_config_missing(self, admin_person, error_id, detail):
        self.config_missing.append(detail)
        return True

    def send_program_error(self, admin_person, error_id, detail):
        self.program_error.append(detail)
        return True


def _exec(sql, params=()):
    """直接操作池表（dedup.db）—— 只用于 UPDATE / DELETE。"""
    return aggregate._rows(sql, params, fetch=False)


def _exec_bot_config(sql, params=()):
    from mailbots_next.core import store
    with store._bot_config_lock:
        conn = store.get_bot_config_connection()
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()


def _insert_forward_row(message_id, fid="fid-batch-1"):
    """模拟 flush 第 6.5 步已经写过的 forward_log 行（崩溃恢复判据用）。"""
    _exec_bot_config(
        "INSERT OR REPLACE INTO forward_log (forward_id, message_id, sent_at)"
        " VALUES (?,?,datetime('now'))", (fid, message_id))


def _default_fetch(items, batch):
    """默认的「按 uid 取附件」桩：每票一个 PDF 字节，还箱令无附件。"""
    if str(batch.get("email_type") or "") == "retbox":
        return {int(i["id"]): [] for i in items}
    return {
        int(i["id"]): [{
            "filename": f"{i.get('release_no') or 'doc'}.pdf",
            "content_type": "application/pdf",
            "size": 20,
            "payload": b"%PDF-1.4 stub-" + str(int(i["id"])).encode(),
        }]
        for i in items
    }


def _seed(n=1, company="保时达", train="WB943", email_type="release",
          note=RELEASE_NOTE, validity=RELEASE_VALID, boxes=None, uid_base=1000):
    """往池里塞 n 票（同公司 + 同班列号 + 同类型 ⇒ 同一批）。"""
    pairs = []
    for i in range(n):
        box = boxes[i] if boxes else ""
        pairs.append(aggregate.pool_add(
            company=company,
            train_no=train,
            email_type=email_type,
            message_id=f"<m{i}@test>",
            account="maoxiaoyang@cqtransit.com",
            folder="其他文件夹/草单",
            uid=uid_base + i,
            customer_code=f"CQWLJT2611{i:04d}-VXN",
            release_no=f"0156-335969{10 + i}-1",
            code_no=str(49 + i),
            box_no=box,
            note=note,
            validity=validity,
            link="" if email_type != "retbox" else "https://365.kdocs.cn/folder/coCfRLteOihh",
            retbox_doc="" if email_type != "retbox" else "957班还箱令.pdf",
        ))
    return pairs


def _latest_batch():
    rows = aggregate._rows("SELECT * FROM release_batch ORDER BY rowid DESC LIMIT 1")
    return rows[0]


def _get_batch(batch_id):
    return aggregate._rows("SELECT * FROM release_batch WHERE batch_id=?", (batch_id,))[0]


def _items(batch_id):
    return aggregate._rows("SELECT * FROM release_item WHERE batch_id=? ORDER BY id", (batch_id,))


def _age(batch_id, secs, field="last_item_at"):
    cut = aggregate._ts(datetime.now() - timedelta(seconds=secs))
    _exec(f"UPDATE release_batch SET {field}=? WHERE batch_id=?", (cut, batch_id))


def _set_status(batch_id, status):
    _exec("UPDATE release_batch SET status=? WHERE batch_id=?", (status, batch_id))


def _html_of(msg):
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf-8", "replace")
    return ""


def _attachment_names(msg):
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        if fn:
            out.append(fn)
    return out


def load_zip_payload(msg):
    for part in msg.walk():
        if part.is_multipart():
            continue
        if (part.get_filename() or "").endswith(".zip"):
            return part.get_payload(decode=True)
    raise AssertionError("no zip attachment")


def _zip_names(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return zf.namelist()


# ---------------------------------------------------------------- 夹具

@pytest.fixture(autouse=True)
def ctx(monkeypatch):
    """全量打桩 + 池表清零（每个用例互不影响）。"""
    calls = {"smtp": [], "seen": []}
    notifier = _FakeNotifier()

    def fake_send(msg, sender_email, sender_password, to_list, cc_list=None, mail_from=None):
        calls["smtp"].append({
            "msg": msg,
            "subject": msg.get("Subject", ""),
            "to": list(to_list),
            "cc": list(cc_list or []),
            "from": sender_email,
        })
        return {"success": True, "refused": {}}

    monkeypatch.setattr(aggregate, "send_smtp", fake_send)
    monkeypatch.setattr(aggregate, "mark_seen",
                        lambda account, folder, uid: calls["seen"].append((account, folder, uid)) or True)
    monkeypatch.setattr(aggregate, "get_recipients",
                        lambda company, scope="company": (["cust@example.test"], ["cc@example.test"]))
    monkeypatch.setattr(aggregate, "get_responsible_person",
                        lambda company: "owner@example.test")
    monkeypatch.setattr(aggregate, "get_sender_by_email",
                        lambda email: (email, "pwd-x") if email else None)
    monkeypatch.setattr(aggregate, "_fetch_item_attachments", _default_fetch)
    monkeypatch.setattr(aggregate, "get_notifier", lambda: notifier)

    aggregate.init_release_tables()
    _exec("DELETE FROM release_item")
    _exec("DELETE FROM release_batch")
    _exec_bot_config("DELETE FROM forward_log WHERE message_id LIKE 'BATCH::%'")

    yield SimpleNamespace(calls=calls, notifier=notifier)

    _exec("DELETE FROM release_item")
    _exec("DELETE FROM release_batch")
    _exec_bot_config("DELETE FROM forward_log WHERE message_id LIKE 'BATCH::%'")


@pytest.fixture
def forward_writes(monkeypatch):
    """观察「写 forward_log」这件事本身。

    `_record_forward` 第一句是 `if not config.is_live(): return`（act.py:184），
    test 模式下**一行都不写**，不打桩永远观察不到"forward_log 1 行"。

    桩打在 `mailbots_next.core.act.write_forward_log` —— 这是 `_record_forward`
    的真实写入点，也是 aggregate 实际走的调用链（aggregate 按 A13 要求调
    `act._record_forward`，并不自己写库，所以打 aggregate 命名空间里的
    `write_forward_log` 观察不到任何东西）。
    同时把 `mailbots_next.config.is_live` 局部改成 True 让那道门放行：
    不动环境变量（不用 setenv 全局切 live），只影响本用例。
    """
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.act as act_mod

    rows = []
    monkeypatch.setattr(act_mod, "write_forward_log",
                        lambda *a, **k: rows.append({"args": a, "kwargs": k}))
    monkeypatch.setattr(cfg_mod, "is_live", lambda: True)
    return rows


# ---------------------------------------------------------------- 1~4 三闸

def test_01_quiet_gate_fires_after_120s():
    """55 票入池，最后一票静默 120s 后到期。"""
    _seed(55)
    b = _latest_batch()
    assert b["item_count"] == 55
    assert b["status"] == "pending"

    _age(b["batch_id"], 130)
    due = aggregate.list_due_batches()
    assert [d["batch_id"] for d in due] == [b["batch_id"]]


def test_02_quiet_gate_not_yet():
    """最后一票只静默 60s：不发。"""
    _seed(2)
    b = _latest_batch()
    _age(b["batch_id"], 60)
    assert aggregate.list_due_batches() == []


def test_03_count_gate_fires_without_quiet():
    """条数闸：200 票不等静默立即到期。"""
    _seed(200)
    b = _latest_batch()
    assert b["item_count"] == 200
    due = aggregate.list_due_batches()
    assert [d["batch_id"] for d in due] == [b["batch_id"]]


def test_04_hard_cap_fires():
    """硬上限：首票已过 1800s，即使最后一票刚来也要放行。"""
    _seed(2)
    b = _latest_batch()
    _age(b["batch_id"], 1900, field="first_item_at")
    due = aggregate.list_due_batches()
    assert [d["batch_id"] for d in due] == [b["batch_id"]]


# ---------------------------------------------------------------- 5~6 去重与后缀

def test_05_duplicate_item_ignored():
    """同 (客编, 数字编码, 箱号) 再来：返回 (False, True)，item_count 不变。"""
    _seed(1)
    b = _latest_batch()
    added, is_dup = aggregate.pool_add(
        company="保时达", train_no="WB943", email_type="release",
        message_id="<dup@test>", account="maoxiaoyang@cqtransit.com",
        folder="其他文件夹/草单", uid=2001,
        customer_code="CQWLJT26110000-VXN", release_no="0156-33596910-1",
        code_no="49", box_no="", note=RELEASE_NOTE, validity=RELEASE_VALID,
    )
    assert (added, is_dup) == (False, True)
    assert _get_batch(b["batch_id"])["item_count"] == 1
    assert len(_items(b["batch_id"])) == 1


def test_06_new_batch_after_sent_gets_suffix():
    """批次已发出后再来同一公司同班列：新建批次，id 带 -2 后缀，不并回老批次。"""
    _seed(1)
    old = _latest_batch()
    _age(old["batch_id"], 130)
    assert aggregate.flush_batch(old) is True
    assert _get_batch(old["batch_id"])["status"] == "sent"

    _seed(1, uid_base=3000)
    new = _latest_batch()
    assert new["batch_id"] != old["batch_id"]
    assert new["batch_id"] == old["batch_id"] + "-2"
    assert new["status"] == "pending"
    assert _get_batch(old["batch_id"])["item_count"] == 1


# ---------------------------------------------------------------- 7~9 正文

def test_07_html_row_count():
    """55 票 → HTML 里 55 个数据行（表头另算）。"""
    _seed(55)
    b = _latest_batch()
    body = aggregate._build_html(b, _items(b["batch_id"]))
    assert body.count("<tr>") == 56           # 1 表头 + 55 行
    assert aggregate.build_subject(b, 55) == "渝新欧放箱｜WB943｜55 票"


def test_08_note_column_only_when_differs():
    """55 票 note 全相同 → 表格下方只写一次；2 票不同 → 加「备注」列逐条列。"""
    _seed(5)
    b = _latest_batch()
    items = _items(b["batch_id"])

    same = aggregate._build_html(b, items)
    assert "备注" not in same                   # 全相同 ⇒ 不进表格列
    assert same.count("请填写放箱令邮件后面的数字编码") == 1   # 只写一次

    items[0]["note"] = "第一票：请提前预约车队"
    items[1]["note"] = "第二票：周末不派车"
    diff = aggregate._build_html(b, items)
    assert "备注" in diff
    assert "第一票：请提前预约车队" in diff
    assert "第二票：周末不派车" in diff


def test_09_validity_rendered_once():
    """有效期是独立字段：note 里那段被 RE_VALID 剔掉，正文里只出现一次。"""
    _seed(1)
    b = _latest_batch()
    items = _items(b["batch_id"])
    assert items[0]["note"].endswith(RELEASE_VALID)      # 入库保留原文

    body = aggregate._build_html(b, items)
    assert body.count(RELEASE_VALID) == 1
    assert f"<b>有效期：{RELEASE_VALID}</b>" in body


# ---------------------------------------------------------------- 10~11 zip

def test_10_zip_names_one_file_per_item():
    """55 票一票一文件，文件名 `{放箱令号} 【{数字编码}】.pdf`。"""
    _seed(55)
    b = _latest_batch()
    items = _items(b["batch_id"])
    parts = aggregate._build_zip_parts(items, _default_fetch(items, b), b)
    assert len(parts) == 1
    assert parts[0]["filename"] == "WB943_放箱令_" + b["batch_id"].split("|")[-1] + ".zip"
    names = _zip_names(parts[0]["payload"])
    assert len(names) == 55
    assert "0156-33596910-1 【49】.pdf" in names


def test_11_zip_split_keeps_single_mail(monkeypatch, ctx):
    """超 50MB 护栏调小到 1MB：多个 _partN.zip，但**只有一封邮件**。"""
    _seed(3)
    b = _latest_batch()
    items = _items(b["batch_id"])
    big = {
        int(it["id"]): [{"filename": "x.pdf", "content_type": "application/pdf",
                         "size": 600 * 1024, "payload": b"%PDF" + b"0" * (600 * 1024)}]
        for it in items
    }
    monkeypatch.setattr(aggregate, "_fetch_item_attachments", lambda i, bb: big)
    monkeypatch.setattr(aggregate.settings, "RELEASE_ZIP_MAX_BYTES", 1024 * 1024)

    _age(b["batch_id"], 130)
    assert aggregate.flush_batch(b) is True

    assert len(ctx.calls["smtp"]) == 1
    names = _attachment_names(ctx.calls["smtp"][0]["msg"])
    assert len(names) >= 2
    assert all(n.endswith(".zip") for n in names)
    assert any("_part1" in n for n in names) and any("_part2" in n for n in names)


# ---------------------------------------------------------------- 12~13 发送结果

def test_12_send_failure_marks_failed_and_skips_bookkeeping(monkeypatch, ctx, forward_writes):
    """send_smtp 失败：failed，不写 forward_log、不标已读。"""
    _seed(2)
    b = _latest_batch()
    monkeypatch.setattr(aggregate, "send_smtp",
                        lambda *a, **k: {"success": False, "error": "stub"})
    _age(b["batch_id"], 130)

    assert aggregate.flush_batch(b) is False
    assert _get_batch(b["batch_id"])["status"] == "failed"
    assert forward_writes == []
    assert ctx.calls["seen"] == []
    assert all(it["status"] == "pooled" for it in _items(b["batch_id"]))


def test_13_send_success_marks_sent_and_marks_seen(monkeypatch, ctx, forward_writes):
    """发送成功：sent + item 全 sent + forward_log 1 行 + 整批补标已读。

    同时用侧记顺序锁住 A14b：send_smtp → 写 forward_log → 标 sent。
    顺序反了，崩在"发出去但没标 sent"的批次会被启动恢复判成没发 ⇒ 重复发送。
    """
    order = []
    prev_send = aggregate.send_smtp
    monkeypatch.setattr(aggregate, "send_smtp",
                        lambda *a, **k: (order.append("send_smtp"), prev_send(*a, **k))[1])
    import mailbots_next.core.act as act_mod
    prev_log = act_mod.write_forward_log
    monkeypatch.setattr(act_mod, "write_forward_log",
                        lambda *a, **k: (order.append("write_forward_log"), prev_log(*a, **k))[1])
    prev_mark = aggregate._mark_batch_sent
    monkeypatch.setattr(aggregate, "_mark_batch_sent",
                        lambda *a, **k: (order.append("mark_sent"), prev_mark(*a, **k))[1])

    _seed(55)
    b = _latest_batch()
    _age(b["batch_id"], 130)

    assert aggregate.flush_batch(b) is True
    assert order == ["send_smtp", "write_forward_log", "mark_sent"]

    after = _get_batch(b["batch_id"])
    assert after["status"] == "sent"
    assert after["sent_at"]
    assert after["forward_id"]
    assert all(it["status"] == "sent" for it in _items(b["batch_id"]))
    assert len(forward_writes) == 1
    assert forward_writes[0]["args"][1] == f"BATCH::{b['batch_id']}"     # message_id
    assert len(ctx.calls["seen"]) == 55
    assert ctx.calls["smtp"][0]["subject"] == "渝新欧放箱｜WB943｜55 票"
    assert attachments_are_zip(ctx.calls["smtp"][0]["msg"])


def attachments_are_zip(msg):
    names = _attachment_names(msg)
    return len(names) == 1 and names[0].endswith(".zip")


# ---------------------------------------------------------------- 14 重启恢复（pending）

def test_14_startup_flushes_pending_only_once(ctx):
    """pending 未 flush 就重启：启动恢复发出，且只发一次。"""
    _seed(3)
    b = _latest_batch()
    _age(b["batch_id"], 130)

    stats = aggregate.flush_due_on_startup()
    assert stats["flushed"] == 1
    assert len(ctx.calls["smtp"]) == 1
    assert _get_batch(b["batch_id"])["status"] == "sent"

    stats2 = aggregate.flush_due_on_startup()
    assert stats2["flushed"] == 0
    assert len(ctx.calls["smtp"]) == 1


# ---------------------------------------------------------------- 15 空附件票

def test_15_item_without_attachment_still_in_table(monkeypatch, ctx):
    """某票没附件：表格仍有该行、zip 少一个文件、有告警。"""
    _seed(3)
    b = _latest_batch()
    items = _items(b["batch_id"])

    def fetch_no_att_for_first(its, batch):
        out = _default_fetch(its, batch)
        out[int(items[0]["id"])] = []
        return out

    monkeypatch.setattr(aggregate, "_fetch_item_attachments", fetch_no_att_for_first)
    _age(b["batch_id"], 130)
    assert aggregate.flush_batch(b) is True

    msg = ctx.calls["smtp"][0]["msg"]
    body = _html_of(msg)
    assert body.count("<tr>") == 4                      # 1 表头 + 3 行（含无附件那票）
    assert len(_attachment_names(msg)) == 1
    assert len(_zip_names(load_zip_payload(msg))) == 2  # 少一个文件
    assert ctx.notifier.program_error, "空附件票必须告警，不能静默少一个"


# ---------------------------------------------------------------- 16 batch_id 含类型

def test_16_batch_id_carries_email_type(ctx):
    """同公司同班列先 release 后 release_cancel：两个不同批次，取消件标题带【取消】。"""
    _seed(1, email_type="release")
    rel = _latest_batch()

    _seed(1, email_type="release_cancel", uid_base=4000)
    cancel = _latest_batch()

    assert rel["batch_id"] != cancel["batch_id"]
    assert "|release|" in rel["batch_id"]
    assert "|release_cancel|" in cancel["batch_id"]
    assert cancel["email_type"] == "release_cancel"

    _age(cancel["batch_id"], 130)
    assert aggregate.flush_batch(cancel) is True
    assert ctx.calls["smtp"][0]["subject"].startswith("【取消】")
    assert _html_of(ctx.calls["smtp"][0]["msg"]).find("已取消分配") > 0


# ---------------------------------------------------------------- 17 flushing 抢占

def test_17_flushing_is_invisible_to_gates_and_pool():
    """flushing 的批次：list_due_batches 不返回；pool_add 另建（不并进去）。"""
    _seed(2)
    b = _latest_batch()
    _set_status(b["batch_id"], "flushing")
    _age(b["batch_id"], 999)

    assert aggregate.list_due_batches() == []

    _seed(1, uid_base=5000)
    new = _latest_batch()
    assert new["batch_id"] == b["batch_id"] + "-2"
    assert new["status"] == "pending"
    assert len(_items(b["batch_id"])) == 2      # flushing 那批没被塞进新票


# ---------------------------------------------------------------- 18 取附件失败 ⇒ 整批 failed

def test_18_fetch_failure_fails_whole_batch(monkeypatch, ctx, forward_writes):
    """任一票取附件失败：整批 failed，不发半批（send_smtp 一次都没被调）。"""
    _seed(55)
    b = _latest_batch()

    def boom(items, batch):
        raise RuntimeError("uid 9999 not found")

    monkeypatch.setattr(aggregate, "_fetch_item_attachments", boom)
    _age(b["batch_id"], 130)

    assert aggregate.flush_batch(b) is False
    assert _get_batch(b["batch_id"])["status"] == "failed"
    assert ctx.calls["smtp"] == []
    assert forward_writes == []
    assert all(it["status"] == "pooled" for it in _items(b["batch_id"]))
    assert ctx.notifier.program_error


# ---------------------------------------------------------------- 19~20 崩溃恢复（flushing）

def test_19_flushing_without_forward_log_resets_and_resends(ctx):
    """崩在发送之前：flushing + forward_log 无记录 ⇒ 重置 pending 并正常发出（不丢）。"""
    _seed(2)
    b = _latest_batch()
    _set_status(b["batch_id"], "flushing")
    _age(b["batch_id"], 130)

    stats = aggregate.flush_due_on_startup()
    assert stats["reset"] == 1
    assert stats["recovered_sent"] == 0
    assert len(ctx.calls["smtp"]) == 1
    after = _get_batch(b["batch_id"])
    assert after["status"] == "sent"
    assert all(it["status"] == "sent" for it in _items(b["batch_id"]))


def test_20_flushing_with_forward_log_is_not_resent(ctx):
    """崩在发送之后、标 sent 之前：forward_log 有 BATCH::<bid> ⇒ 直接标 sent，绝不重发。"""
    _seed(2)
    b = _latest_batch()
    _set_status(b["batch_id"], "flushing")
    _insert_forward_row(f"BATCH::{b['batch_id']}", fid="fid-recovered")

    stats = aggregate.flush_due_on_startup()
    assert stats["recovered_sent"] == 1
    assert stats["flushed"] == 0

    after = _get_batch(b["batch_id"])
    assert after["status"] == "sent"
    assert after["forward_id"] == "fid-recovered"
    assert all(it["status"] == "sent" for it in _items(b["batch_id"]))
    assert len(ctx.calls["seen"]) == 2          # 补标已读
    assert ctx.calls["smtp"] == []              # 🔴 一次都没发


# ---------------------------------------------------------------- 21 收件人为空

def test_21_empty_recipients_fails_batch(monkeypatch, ctx):
    """收件人为空 ⇒ failed 不发（否则 send_smtp([]) 假成功 ⇒ 标 sent 但没发出去）。"""
    _seed(1)
    b = _latest_batch()
    monkeypatch.setattr(aggregate, "get_recipients", lambda company, scope="company": ([], []))
    _age(b["batch_id"], 130)

    assert aggregate.flush_batch(b) is False
    assert _get_batch(b["batch_id"])["status"] == "failed"
    assert ctx.calls["smtp"] == []
    assert ctx.calls["seen"] == []
    # 任务 16：E2 配置缺失不再逐条弹企微，改为记进当日汇总项（每日邮件第三节）
    from mailbots_next.core.notify import get_issues
    items = [i for i in get_issues()
             if i["kind"] == "config_missing" and i["code"] == "release_batch_config"]
    assert items, "收件人为空要按 E2 配置缺失记账（邮件第三节）"
    assert "保时达" in items[0]["detail"]
    assert ctx.notifier.config_missing == []
