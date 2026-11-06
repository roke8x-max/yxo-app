# -*- coding: utf-8 -*-
"""放箱令 / 还箱令 —— 复制归档与每日核对删除（任务 12 第四阶段）。

用例 #1–#24，编号与 02_TASK.md §六 一致（唯一真相源）。

不连 IMAP、不发真信、不读真实库：
  * `archive._new_conn` 换成一个 FakeIMAP（模拟文件夹 / UID / UIDVALIDITY / COPY /
    STORE / EXPUNGE），`login` 的调用次数直接被记录（#24 就是断言这个）；
  * `config.is_live` 只在用例内改成 True（非 live 的用例显式改回 False）；
  * `archive.mark_seen` / `archive._alert` / `archive.get_notifier` 全部打桩；
  * 记录落 conftest 指到的临时 dedup.db，每个用例前后清空。

桩一律打在 `mailbots_next.core.archive` 的命名空间上：archive.py 是
`from .ingest import mark_seen` 这种写法，函数对象在导入那一刻就绑进了 archive
的命名空间，改定义方对 archive 里的引用无效（任务 10 踩过同一个坑）。
"""
from types import SimpleNamespace

import pytest

import mailbots_next.config as cfg
from mailbots_next.core import aggregate, archive

ACCT = "maoxiaoyang@cqtransit.com"
SRC = "运单草单"
RELEASE = "放箱令"
RETBOX = "还箱令"


def _f(name):
    """显示名 → 服务器 mailbox 名（与生产代码同一条路径）。"""
    return archive._imap_folder(name)


def _norm(mid):
    s = "".join(str(mid or "").split())
    return s.strip("<>").lower()


# ---------------------------------------------------------------- 假 IMAP

class FakeIMAP:
    """够用的 IMAP 桩：只实现 archive 用到的命令，并把"做了什么"全记下来。"""

    def __init__(self):
        self.folders = {}              # {server_folder: [(uid, message_id), ...]}
        self.uidvalidity = {}          # {server_folder: int | None}
        self.selected = None
        self.untagged_responses = {}
        self.login_count = 0
        self.logout_count = 0
        self.created = []
        self.copy_calls = []           # [(uid, target_server_folder)]
        self.store_calls = []          # [(uids, flag, flag_list)]
        self.expunge_calls = []        # [uids]
        self.fetch_calls = []
        self.raw_expunge_calls = 0     # 不带 uid 的 expunge（红线）
        self.close_calls = 0           # CLOSE（红线）
        self.fail_copy = False
        self.fail_store = False
        self.fail_expunge = False
        self._next_uid = 900

    # -- 夹具便捷方法 --
    def set_folders(self, mapping):
        self.folders = {_f(k): list(v) for k, v in mapping.items()}
        return self

    def set_uidvalidity(self, mapping):
        self.uidvalidity = {_f(k): v for k, v in mapping.items()}
        return self

    def uids_in(self, display):
        return [u for u, _ in self.folders.get(_f(display), [])]

    def mids_in(self, display):
        return [m for _, m in self.folders.get(_f(display), [])]

    # -- IMAP 表面 --
    def login(self, user, password):
        self.login_count += 1
        return "OK", [b"logged in"]

    def logout(self):
        self.logout_count += 1
        return "BYE", [b"bye"]

    def select(self, folder, readonly=False):
        if folder not in self.folders:
            return "NO", [b"no such mailbox"]
        self.selected = folder
        self.untagged_responses = {}
        validity = self.uidvalidity.get(folder)
        if validity:
            self.untagged_responses["UIDVALIDITY"] = [str(validity).encode()]
        return "OK", [str(len(self.folders[folder])).encode()]

    def create(self, folder):
        self.created.append(folder)
        self.folders.setdefault(folder, [])
        return "OK", [b"created"]

    def expunge(self, *args):
        self.raw_expunge_calls += 1
        return "OK", [b"expunged"]

    def close(self):
        self.close_calls += 1
        return "OK", [b"closed"]

    def uid(self, command, *args):
        cmd = str(command).upper()
        if cmd == "SEARCH":
            return self._search([a for a in args if a is not None])
        if cmd == "COPY":
            return self._copy(int(str(args[0])), str(args[1]))
        if cmd == "STORE":
            return self._store(str(args[0]), str(args[1]), str(args[2]))
        if cmd == "EXPUNGE":
            return self._expunge_uids(str(args[0]))
        if cmd == "FETCH":
            return self._fetch(int(str(args[0])))
        return "BAD", [b"unsupported"]

    def _search(self, crit):
        cur = self.selected
        rows = self.folders.get(cur, [])
        if not crit:
            return "OK", [b""]
        head = str(crit[0]).upper()
        if head == "UID":
            want = int(str(crit[1]))
            hit = [u for u, _ in rows if u == want]
            return "OK", [b" ".join(str(u).encode() for u in hit)]
        if head == "HEADER":
            want = _norm(str(crit[2]).strip('"'))
            hit = [u for u, mid in rows if _norm(mid) == want]
            return "OK", [b" ".join(str(u).encode() for u in hit)]
        return "OK", [b""]

    def _copy(self, uid, target):
        if self.fail_copy:
            return "NO", [b"copy failed"]
        row = next((r for r in self.folders.get(self.selected, []) if r[0] == uid), None)
        if row is None:
            return "NO", [b"no such uid"]
        self._next_uid += 1
        self.folders.setdefault(target, []).append((self._next_uid, row[1]))
        self.copy_calls.append((uid, target))
        resp = f"[COPYUID {self.uidvalidity.get(self.selected) or 1} {uid} {self._next_uid}]"
        return "OK", [resp.encode()]

    def _store(self, uids, flag, flag_list):
        if self.fail_store:
            return "NO", [b"store failed"]
        self.store_calls.append((uids, flag, flag_list))
        return "OK", [b"stored"]

    def _expunge_uids(self, uids):
        if self.fail_expunge:
            raise RuntimeError("EXPUNGE boom")
        self.expunge_calls.append(uids)
        want = {int(x) for x in uids.split(",") if x.strip()}
        cur = self.selected
        self.folders[cur] = [r for r in self.folders.get(cur, []) if r[0] not in want]
        return "OK", [b"expunged"]

    def _fetch(self, uid):
        self.fetch_calls.append(uid)
        row = next((r for r in self.folders.get(self.selected, []) if r[0] == uid), None)
        if row is None:
            return "OK", [None]
        payload = b"Message-ID: " + str(row[1]).encode() + b"\r\n\r\n"
        head = b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {%d}" % len(payload)
        return "OK", [(head, payload), b")"]


class FakeNotifier:
    def __init__(self):
        self.alerts = []
        self.summaries = []

    def send_program_error(self, admin_person, error_id, detail):
        self.alerts.append(detail)
        return True

    def notify(self, notify_type, recipients, content, error_id=None):
        self.summaries.append((notify_type, content))
        return True


# ---------------------------------------------------------------- 库/夹具工具

def _all():
    return archive._rows("SELECT * FROM archive_log ORDER BY id")


def _one(mid, target=RELEASE, account=ACCT):
    rows = [r for r in _all()
            if r["message_id"] == mid and r["target_folder"] == target
            and r["account"] == account]
    assert len(rows) == 1, f"expected exactly one row for {mid}, got {rows}"
    return rows[0]


def _seed(mid="<m1@test>", uid=101, status="copied", validity=1000,
          account=ACCT, src=SRC, target=RELEASE, age_min=120):
    """直接落一条 archive_log（模拟"第一段已经跑过"）。

    `copied_at` 用 SQLite 的 datetime('now') 口径（UTC）写 —— 与生产代码同一条时钟，
    混用 Python 本地时间会让时间闸永远不成立。
    """
    archive._write_record(account, src, uid, mid, target, status="copied",
                          src_uidvalidity=validity)
    archive._exec(
        "UPDATE archive_log SET status=?, copied_at=datetime('now', ?)"
        " WHERE account=? AND message_id=? AND target_folder=?",
        (status, f"-{age_min} minutes", account, mid, target),
    )
    return _one(mid, target, account)


def _seed_bulk(n, validity=1000):
    """批量落 n 条 copied 记录（#14 的 600 条用）。

    走**一个**连接 executemany，而不是循环 `_seed()`：SQLite 每条语句都开连接 +
    设 WAL 会把 600 条拖成 70 秒，测试没有理由为"建数据"付这个代价。
    生产路径仍走 `_write_record`（见 #1~#23）。
    """
    with archive._db_lock:
        conn = archive.get_dedup_connection()
        try:
            conn.executemany(
                "INSERT INTO archive_log"
                " (message_id, account, src_folder, src_uid, src_uidvalidity,"
                "  target_folder, copy_uid, copied_at, status, fail_reason)"
                " VALUES (?,?,?,?,?,?,0, datetime('now','-120 minutes'),'copied','')",
                [(f"<m{i}@test>", ACCT, SRC, 1000 + i, validity, RELEASE)
                 for i in range(n)],
            )
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def env(monkeypatch):
    """live 模式 + 假 IMAP + 告警/已读打桩 + 空表。"""
    srv = FakeIMAP()
    notifier = FakeNotifier()
    seen = []
    monkeypatch.setattr(cfg, "is_live", lambda: True)
    monkeypatch.setattr(archive, "_new_conn", lambda: srv)
    monkeypatch.setattr(archive, "mark_seen", lambda a, f, u: seen.append((a, f, u)) or True)
    monkeypatch.setattr(archive, "_alert", lambda detail: notifier.alerts.append(detail))
    monkeypatch.setattr(archive, "get_notifier", lambda: notifier)

    archive.init_table()
    archive._exec("DELETE FROM archive_log")
    yield SimpleNamespace(srv=srv, notifier=notifier, seen=seen)
    archive._exec("DELETE FROM archive_log")


def _copy(env, mid="<m1@test>", uid=101, target=RELEASE, src=SRC, account=ACCT):
    return archive.copy_after_forward(account, src, uid, mid, target)


# ---------------------------------------------------------------- #1~#6 复制段

def test_01_copy_success(env):
    """#1 复制成功：调了 UID COPY，写 copied，目标夹多 1 封，源夹仍在。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert _copy(env) == "copied"

    assert env.srv.copy_calls == [(101, _f(RELEASE))]
    row = _one("<m1@test>")
    assert row["status"] == "copied"
    assert row["src_uidvalidity"] == 1000          # 真实值，不是 0
    assert row["src_uid"] == 101
    assert env.srv.mids_in(RELEASE) == ["<m1@test>"]   # 目标夹多 1 封
    assert env.srv.uids_in(SRC) == [101]              # 源夹仍在（一封都没删）


def test_02_idempotent(env):
    """#2 幂等：同一封调两次 → 只 COPY 一次，archive_log 1 行。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert _copy(env) == "copied"
    assert _copy(env) == "exists"

    assert len(env.srv.copy_calls) == 1
    assert len(_all()) == 1
    assert env.srv.mids_in(RELEASE) == ["<m1@test>"]


def test_03_recovered_when_copy_already_there(env):
    """#3 复制前已有副本：不 COPY，补 status='copied'（recovered）。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert _copy(env) == "recovered"

    assert env.srv.copy_calls == []                    # 没有第二次复制
    row = _one("<m1@test>")
    assert row["status"] == "copied"
    assert row["src_uidvalidity"] == 1000              # recovered 也取了真实 UIDVALIDITY
    assert len(env.srv.mids_in(RELEASE)) == 1


def test_04_copy_failure_keeps_source(env):
    """#4 复制失败：failed_copy，源不动，异常不外抛。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})
    env.srv.fail_copy = True

    assert _copy(env) == "failed_copy"

    row = _one("<m1@test>")
    assert row["status"] == "failed_copy"
    assert env.srv.mids_in(RELEASE) == []
    assert env.srv.uids_in(SRC) == [101]
    assert env.notifier.alerts                      # 告警留痕


def test_05_target_folder_created_when_whitelisted(env):
    """#5 目标夹不存在（白名单内）：调了 CREATE。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert _copy(env) == "copied"

    assert env.srv.created == [_f(RELEASE)]
    assert _one("<m1@test>")["status"] == "copied"


def test_06_target_folder_outside_whitelist(env):
    """#6 目标夹不存在（白名单外）：不 CREATE，failed_copy + 告警。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000})

    assert _copy(env, target="非白名单夹") == "failed_target_missing"

    assert env.srv.created == []                       # 一个夹都没建
    assert env.srv.copy_calls == []
    row = _one("<m1@test>", target="非白名单夹")
    assert row["status"] == "failed_copy"
    assert row["fail_reason"] == "target_missing"
    assert env.notifier.alerts


# ---------------------------------------------------------------- #7~#14 核对删除段

def test_07_verified_then_deleted(env):
    """#7 核对命中 → 删除：STORE \\Deleted + UID EXPUNGE，status='deleted'。"""
    _seed()
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert stats["verified"] == 1 and stats["deleted"] == 1
    assert env.srv.store_calls == [("101", "+FLAGS", "(\\Deleted)")]
    assert env.srv.expunge_calls == ["101"]            # UID EXPUNGE，带 uid
    assert env.srv.raw_expunge_calls == 0              # 绝不整夹清理
    assert env.srv.close_calls == 0
    assert _one("<m1@test>")["status"] == "deleted"
    assert _one("<m1@test>")["deleted_at"]             # 只在 EXPUNGE 成功后写
    assert env.srv.uids_in(SRC) == []                  # 源邮件真的没了
    assert env.srv.mids_in(RELEASE) == ["<m1@test>"]   # 副本完好


def test_08_copy_not_found_is_never_deleted(env):
    """#8 核对未命中：failed_verify，没有 STORE/EXPUNGE，告警。"""
    _seed()
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert stats["failed"] == 1 and stats["deleted"] == 0
    assert env.srv.store_calls == [] and env.srv.expunge_calls == []
    row = _one("<m1@test>")
    assert row["status"] == "failed_verify"
    assert row["fail_reason"] == "copy_not_found"
    assert env.notifier.alerts
    assert env.srv.uids_in(SRC) == [101]               # 源邮件还在


def test_09_time_gate(env):
    """#9 时间闸：copied_at 10 分钟前 → 不处理。"""
    _seed(age_min=10)
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert stats["scanned"] == 0
    assert env.srv.store_calls == [] and env.srv.expunge_calls == []
    assert _one("<m1@test>")["status"] == "copied"


def test_10_uidvalidity_changed_stops_whole_group(env):
    """#10 UIDVALIDITY 变化：该组标 failed_verify(uidvalidity_changed)，一封都不删 + 告警一次。"""
    _seed("<m1@test>", uid=101)
    _seed("<m2@test>", uid=102)
    env.srv.set_folders({SRC: [(101, "<m1@test>"), (102, "<m2@test>")],
                         RELEASE: [(901, "<m1@test>"), (902, "<m2@test>")]})
    env.srv.set_uidvalidity({SRC: 2000, RELEASE: 2000})   # 记录里是 1000

    stats = archive.purge_verified()

    assert stats["deleted"] == 0
    assert env.srv.store_calls == [] and env.srv.expunge_calls == []
    for mid in ("<m1@test>", "<m2@test>"):
        assert _one(mid)["status"] == "failed_verify"
        assert _one(mid)["fail_reason"] == "uidvalidity_changed"
    assert len(env.notifier.alerts) == 1                  # 告警一次，不是每条一次
    assert "UIDVALIDITY" in env.notifier.alerts[0]
    assert env.srv.uids_in(SRC) == [101, 102]


def test_11_message_id_mismatch_only_skips_that_one(env):
    """#11 第③闸 Message-ID 不符：该条 skipped + 告警，其他正常删。"""
    _seed("<m1@test>", uid=101)
    _seed("<m2@test>", uid=102)
    # uid 102 已被别的邮件复用
    env.srv.set_folders({SRC: [(101, "<m1@test>"), (102, "<other@test>")],
                         RELEASE: [(901, "<m1@test>"), (902, "<m2@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert _one("<m1@test>")["status"] == "deleted"
    assert _one("<m2@test>")["status"] == "failed_verify"
    assert _one("<m2@test>")["fail_reason"] == "message_id_mismatch"
    assert env.srv.expunge_calls == ["101"]        # 只删核对通过的那一封
    assert env.srv.uids_in(SRC) == [102]
    assert stats["deleted"] == 1 and stats["failed"] == 1
    assert any("复用" in a for a in env.notifier.alerts)


def test_12_empty_message_id(env):
    """#12 message_id 为空：status='skipped'，不 COPY 不删。"""
    env.srv.set_folders({SRC: [(101, "")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert archive.copy_after_forward(ACCT, SRC, 101, "", RELEASE) == "skipped_no_message_id"

    assert env.srv.copy_calls == []
    row = _one("")
    assert row["status"] == "skipped"
    assert row["fail_reason"] == "no_message_id"

    stats = archive.purge_verified()
    assert stats["scanned"] == 0
    assert env.srv.expunge_calls == []


def test_13_non_live_does_nothing(env, monkeypatch):
    """#13 非 live 模式：全程 0 次 COPY、0 次 EXPUNGE，且不写库。"""
    monkeypatch.setattr(cfg, "is_live", lambda: False)
    _seed()
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})
    before = _all()

    assert _copy(env) == "skipped_dry_run"
    stats = archive.purge_verified()

    assert env.srv.copy_calls == []
    assert env.srv.store_calls == []
    assert env.srv.expunge_calls == []
    assert env.srv.login_count == 0                    # 连 IMAP 都不连
    assert stats["scanned"] == 0
    assert _all() == before                            # 一行都没写
    assert env.srv.mids_in(RELEASE) == ["<m1@test>"]


def test_14_purge_limit_500(env):
    """#14 500 上限：600 条待删 → 只处理 500。"""
    _seed_bulk(600)
    env.srv.set_folders({
        SRC: [(1000 + i, f"<m{i}@test>") for i in range(600)],
        RELEASE: [(9000 + i, f"<m{i}@test>") for i in range(600)],
    })
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert stats["scanned"] == 500
    assert stats["deleted"] == 500
    assert len(env.srv.expunge_calls) == 1             # 一次批量清，仍带 uid 列表
    assert len(env.srv.expunge_calls[0].split(",")) == 500
    left = archive._rows("SELECT COUNT(*) AS n FROM archive_log WHERE status='copied'")[0]["n"]
    assert left == 100                                 # 剩下 100 条顺延次日


# ---------------------------------------------------------------- #15~#17 verified 三路重入

def test_15_verified_stuck_is_picked_up_again(env):
    """#15 verified 卡住能被再捡起：源在、ID 一致 → 被处理并删掉。"""
    _seed(status="verified")
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert stats["scanned"] == 1
    assert stats["deleted"] == 1
    assert _one("<m1@test>")["status"] == "deleted"
    assert env.srv.uids_in(SRC) == []


def test_16_verified_but_source_already_gone(env):
    """#16 verified + 源已不存在（fetch 返回空）→ deleted，不告警、不报错。"""
    _seed(status="verified", uid=101)
    env.srv.set_folders({SRC: [], RELEASE: [(901, "<m1@test>")]})   # 源 UID 已不在
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert _one("<m1@test>")["status"] == "deleted"
    assert stats["recovered_deleted"] == 1
    assert stats["failed"] == 0
    assert env.notifier.alerts == []                   # 正常情况，绝不告警
    assert env.srv.store_calls == [] and env.srv.expunge_calls == []


def test_17_verified_but_uid_reused(env):
    """#17 verified + Message-ID 不一致（UID 被复用）→ failed_verify + 告警，绝不删除。"""
    _seed(status="verified", uid=101)
    env.srv.set_folders({SRC: [(101, "<someone-else@test>")],
                         RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    stats = archive.purge_verified()

    assert _one("<m1@test>")["status"] == "failed_verify"
    assert _one("<m1@test>")["fail_reason"] == "message_id_mismatch"
    assert env.srv.store_calls == [] and env.srv.expunge_calls == []
    assert env.srv.uids_in(SRC) == [101]               # 别人的信原样留着
    assert stats["deleted"] == 0 and stats["failed"] == 1
    assert env.notifier.alerts


# ---------------------------------------------------------------- #18~#24 其它红线

def test_18_copy_without_uidvalidity_writes_failed_copy(env):
    """#18 取不到 UIDVALIDITY：failed_copy + no_uidvalidity，没有 copied 行。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: None, RELEASE: 2000})     # SELECT 后没有 UIDVALIDITY

    assert _copy(env) == "failed_no_uidvalidity"

    row = _one("<m1@test>")
    assert row["status"] == "failed_copy"
    assert row["fail_reason"] == "no_uidvalidity"
    assert not archive._rows("SELECT * FROM archive_log WHERE status='copied'")
    assert env.srv.copy_calls == []                          # 宁可不复制，也不留 0
    assert env.notifier.alerts


def test_19_group_covered_is_archived_too(monkeypatch):
    """#19 group-covered 也归档：detail 有两个值表示"已发出"。"""
    from mailbots_next import serve

    calls = []
    monkeypatch.setattr(serve, "execute_action", lambda *a, **k: (True, "group-covered"))
    monkeypatch.setattr(serve, "decide",
                        lambda *a, **k: SimpleNamespace(action="forward", tier="T1", reason="ok"))
    monkeypatch.setattr(archive, "copy_after_forward",
                        lambda *a, **k: calls.append(a) or "copied")

    proc = object.__new__(serve.MailProcessor)
    proc.records = []
    proc.accounts = {ACCT: "pwd"}
    proc.notifier = SimpleNamespace(send_forwarded=lambda *a: True,
                                    send_alarm=lambda *a: True,
                                    send_pending=lambda *a: True)
    routing = SimpleNamespace(success=True, company="保时达", responsible_person="",
                              to_list=["cust@example.test"], cc_list=[], route_source="exact")
    row = SimpleNamespace(row_idx=1, customer_code="CQWLJT2611-VXN", container_no="",
                          raw_data={}, train_no="WB943", email_type="release")

    outcome = proc._process_routing_result(
        routing, row, "release", "<m19@test>", "rk1", b"raw",
        ACCT, SRC, 77, "sender@x", "subject", "date",
    )

    assert outcome == "forwarded"
    assert calls == [(ACCT, SRC, 77, "<m19@test>", RELEASE)]


def test_20_uidvalidity_change_exits_loop(env):
    """#20 UIDVALIDITY 变化后能退出循环：第一次落终态，第二次不再被捞出来。"""
    _seed(validity=1000)
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 2000, RELEASE: 2000})

    first = archive.purge_verified()
    assert first["deleted"] == 0
    assert _one("<m1@test>")["status"] == "failed_verify"
    assert _one("<m1@test>")["fail_reason"] == "uidvalidity_changed"

    second = archive.purge_verified()
    # failed_verify 不在取数范围（status IN ('copied','verified')）⇒ 不再重试
    assert second["scanned"] == 0
    assert second["failed"] == 0 and second["deleted"] == 0
    assert len(env.notifier.alerts) == 1               # 只告警一次，不是天天告警
    assert env.srv.expunge_calls == []


def test_21_expunge_failure_keeps_verified(env):
    """#21 EXPUNGE 失败不标 deleted：STORE 成功、EXPUNGE 抛异常 → 保持 verified。"""
    _seed()
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})
    env.srv.fail_expunge = True

    stats = archive.purge_verified()

    row = _one("<m1@test>")
    assert row["status"] == "verified"                 # 下次重试，不是 deleted
    assert row["deleted_at"] == ""
    assert env.srv.store_calls == [("101", "+FLAGS", "(\\Deleted)")]
    assert stats["deleted"] == 0
    assert env.srv.uids_in(SRC) == [101]               # 邮件还带着 \Deleted 留在原夹


def test_22_failed_copy_replay_repairs(env):
    """#22 failed_copy 重放后能修复：不被 exists 挡住，真的重新 COPY。"""
    _seed(status="failed_copy", validity=0)
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    assert _copy(env) == "copied"

    assert env.srv.copy_calls == [(101, _f(RELEASE))]
    row = _one("<m1@test>")
    assert row["status"] == "copied"
    assert row["src_uidvalidity"] == 1000


def test_23_recovered_without_uidvalidity(env):
    """#23 recovered 路径也取 UIDVALIDITY：取不到就 failed_copy，没有 copied 行。"""
    env.srv.set_folders({SRC: [(101, "<m1@test>")], RELEASE: [(901, "<m1@test>")]})
    env.srv.set_uidvalidity({SRC: None, RELEASE: 2000})

    assert _copy(env) == "failed_no_uidvalidity"

    row = _one("<m1@test>")
    assert row["status"] == "failed_copy"
    assert row["fail_reason"] == "no_uidvalidity"
    assert not archive._rows("SELECT * FROM archive_log WHERE status='copied'")
    assert env.srv.copy_calls == []


def test_24_batch_logs_in_once(env):
    """#24 批量归档只登录一次：55 条 item → login 只被调 1 次（不是 55 次）。"""
    n = 55
    items = [{"account": ACCT, "folder": SRC, "uid": 1000 + i,
              "message_id": f"<m{i}@test>", "id": i} for i in range(n)]
    env.srv.set_folders({SRC: [(1000 + i, f"<m{i}@test>") for i in range(n)], RELEASE: []})
    env.srv.set_uidvalidity({SRC: 1000, RELEASE: 2000})

    aggregate._archive_items(items, {"batch_id": "保时达|WB943|release|20261105",
                                     "email_type": "release"})

    assert env.srv.login_count == 1                    # 🔴 每个账号只 login 一次
    assert len(env.srv.copy_calls) == n
    rows = _all()
    assert len(rows) == n
    assert {r["status"] for r in rows} == {"copied"}
    assert {r["src_uidvalidity"] for r in rows} == {1000}
