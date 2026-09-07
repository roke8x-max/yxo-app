import sqlite3
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from mailbots_next.config import (
    TYPE_ROUTES,
    EmailType,
    get_enabled_types,
    is_type_enabled,
    snapshot,
    get_type_route,
)
from mailbots_next.core.dedup import (
    init_db,
    try_claim,
    release_claim,
    is_claimed,
    add_error,
    get_pending_errors,
    get_manual_errors,
    claim_manual_forward,
    reopen_error,
    purge_old_dedup,
)
from mailbots_next.core.store import (
    init_bot_config_db,
    get_bot_config_connection,
    load_records,
    get_responsible_person,
    get_recipients,
    seed_owner_mapping,
    COMPANY_ALIAS,
)
from mailbots_next.core.log import EmailLogger
from mailbots_next.core.routing import route_row, RoutingResult
from mailbots_next.core.decide import decide, Decision
from mailbots_next.core.extract import extract_email, parse_email
from mailbots_next.core.extractors.draft import DraftExtractor
from mailbots_next.core.extractors.waybill import WaybillExtractor
from mailbots_next.core.extractors.tracing import TracingExtractor
from mailbots_next.core.extractors.dsk import DSKExtractor
from mailbots_next.core.extractors.atb import ATBExtractor


def seed_company_recipients():
    """Seed company recipients (scope='company') for testing."""
    conn = get_bot_config_connection()
    try:
        recipients = {
            "太平洋": {"to": ["tp@test.com"], "cc": ["tp_cc@test.com"]},
            "港九港铁": {"to": ["gj@test.com"], "cc": []},
            "东盟": {"to": ["dm@test.com"], "cc": []},
            "同程配": {"to": ["tcp@test.com"], "cc": []},
            "中欧木业": {"to": ["zom@test.com"], "cc": []},
            "沙坪坝": {"to": ["spb@test.com"], "cc": []},
            "保时达": {"to": ["bsd@test.com"], "cc": []},
        }
        for company, data in recipients.items():
            conn.execute(
                """INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra)
                   VALUES (?, 'company', ?, ?, ?, '{}')""",
                ("all", company, json.dumps(data["to"]), json.dumps(data["cc"])),
            )
        conn.commit()
    finally:
        conn.close()


def seed_train_companies():
    """Seed train companies (scope='train') for testing."""
    conn = get_bot_config_connection()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO bot_config (bot, scope, key, extra)
               VALUES ('tracking', 'train', ?, ?)""",
            ("793", json.dumps({"companies": ["太平洋", "东盟"]})),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_minimal_yxo(path):
    """Minimal business DB so code reading yxo.db works against an isolated
    file: records schema + the two standard seed rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE records (id INTEGER PRIMARY KEY, 客户编码 TEXT, 箱号 TEXT,"
            " 班列号 TEXT, 目的站 TEXT, 开票子公司名称 TEXT, 本地货源公司 TEXT,"
            " 状态 TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
        )
        conn.executemany(
            "INSERT INTO records (客户编码, 箱号, 班列号, 目的站, 开票子公司名称,"
            " 本地货源公司, 状态, is_deleted, dsk, ATB)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                ("CQWLJT260810001-SPB", "CICU1000001", "WB1", "测试站",
                 "太平洋", "", "正常", 0, "", ""),
                ("CQWLJT260810002-VXN", "CICU1000002", "WB1", "测试站",
                 "东盟", "", "正常", 0, "", ""),
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def test_dbs(monkeypatch, tmp_path):
    """Per-test DB isolation: every path constant is rebound to tmp_path, so
    no test can observe another test's rows (import-time bindings are patched
    on each holding module, not just config)."""
    import mailbots_next.config as cfg_mod
    import mailbots_next.core.store as store_mod
    import mailbots_next.core.dedup as dedup_mod
    import mailbots_next.core.notify as notify_mod

    mapping = [
        ("YXO_DB_PATH", "yxo.db"),
        ("BOT_CONFIG_DB_PATH", "bot_config.db"),
        ("DEDUP_DB_PATH", "dedup.db"),
        ("DRAFT_NUMS_DB_PATH", "draft_nums.db"),
    ]
    for name, fname in mapping:
        target = tmp_path / fname
        for mod in (cfg_mod, store_mod, dedup_mod):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, target)
    monkeypatch.setattr(
        notify_mod, "DAILY_COUNTERS_PATH", tmp_path / "daily_counters.json")

    _seed_minimal_yxo(tmp_path / "yxo.db")
    init_db()
    init_bot_config_db()
    seed_owner_mapping()
    seed_company_recipients()
    seed_train_companies()
    yield


# Check if xlwt is available
try:
    import xlwt
    HAS_XLWT = True
except ImportError:
    HAS_XLWT = False


class TestConfigProvider:
    def test_snapshot_contains_required_fields(self):
        snap = snapshot()
        assert snap.mode in ("test", "live")
        assert isinstance(snap.type_routes, list)
        assert len(snap.type_routes) == 5
        assert isinstance(snap.enabled_types, dict)
        assert "accounts" in snap.__dict__

    def test_type_routes_priority_order(self):
        priorities = [r["priority"] for r in TYPE_ROUTES]
        assert priorities == sorted(priorities)

    def test_enabled_types_defaults(self):
        enabled = get_enabled_types()
        for t in EmailType:
            assert t.value in enabled
            assert enabled[t.value] is True

    def test_get_type_route(self):
        route = get_type_route("draft")
        assert route is not None
        assert route["type"].value == "draft"

    def test_company_alias_mapping(self):
        assert COMPANY_ALIAS["公运沙坪坝"] == "沙坪坝"
        assert COMPANY_ALIAS["重轮太平洋"] == "太平洋"


class TestDedup:
    def test_try_claim_success(self):
        result = try_claim("msg_123")
        assert result is True

    def test_try_claim_duplicate(self):
        try_claim("msg_123")
        result = try_claim("msg_123")
        assert result is False

    def test_try_claim_with_row_key(self):
        result = try_claim("msg_123", "row_1")
        assert result is True
        result2 = try_claim("msg_123", "row_1")
        assert result2 is False
        result3 = try_claim("msg_123", "row_2")
        assert result3 is True

    def test_release_claim(self):
        try_claim("msg_123")
        assert is_claimed("msg_123")
        release_claim("msg_123")
        assert not is_claimed("msg_123")

    def test_error_queue_add_and_get(self):
        eid = add_error("msg_1", "row_1", "extract", "PARSE_ERROR", "Failed to parse", "err_001")
        assert eid > 0
        pending = get_pending_errors(10)
        assert len(pending) == 1
        assert pending[0]["message_id"] == "msg_1"
        assert pending[0]["stage"] == "extract"

    def test_claim_manual_forward(self):
        eid = add_error("msg_1", "row_1", "act", "FORWARD_FAILED", "SMTP failed", "err_002")
        result = claim_manual_forward(eid)
        assert result is True
        result2 = claim_manual_forward(eid)
        assert result2 is False

        # After claiming, status should be 'resolved', not 'manual'
        from mailbots_next.config import DEDUP_DB_PATH as _LIVE_DEDUP_DB
        conn = sqlite3.connect(_LIVE_DEDUP_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM error_queue WHERE id=?", (eid,))
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "resolved"

    def test_reopen_error(self):
        eid = add_error("msg_1", "row_1", "act", "FORWARD_FAILED", "SMTP failed", "err_003")
        claim_manual_forward(eid)
        result = reopen_error(eid)
        assert result is True

        manual = get_manual_errors(10)
        target = [e for e in manual if e["id"] == eid]
        assert len(target) == 1
        assert target[0]["status"] == "manual"
        assert target[0]["attempt_count"] == 0


class TestStore:
    def test_seed_owner_mapping(self):
        seed_owner_mapping()
        conn = get_bot_config_connection()
        try:
            rows = conn.execute("SELECT key, extra FROM bot_config WHERE scope='owner' AND bot='all'").fetchall()
            assert len(rows) == 7
            for row in rows:
                import json
                extra = json.loads(row["extra"])
                assert "email" in extra
                assert "@cqtransit.com" in extra["email"]
        finally:
            conn.close()

    def test_get_responsible_person(self):
        seed_owner_mapping()
        person = get_responsible_person("太平洋")
        assert person == "maoxiaoyang@cqtransit.com"
        person = get_responsible_person("东盟")
        assert person == "yangyawen@cqtransit.com"
        person = get_responsible_person("不存在的公司")
        assert person is None

    def test_get_recipients(self):
        conn = get_bot_config_connection()
        try:
            import json
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (bot, scope, key, to_addrs, cc_addrs, extra) VALUES (?, ?, ?, ?, ?, ?)",
                ("all", "company", "太平洋", json.dumps(["test@example.com"]), json.dumps(["cc@example.com"]), "{}"),
            )
            conn.commit()
        finally:
            conn.close()

        to, cc = get_recipients("太平洋")
        assert "test@example.com" in to
        assert "cc@example.com" in cc


class TestRouting:
    def test_route_draft_exact_match(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(
            row_idx=0,
            email_type=EmailType.DRAFT,
            customer_code="CQWLJT260810001",
            container_no="CICU1234567",
        )
        records = [{
            "id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1234567",
            "company": "太平洋", "状态": "正常", "is_deleted": 0,
        }]
        result = route_row(EmailType.DRAFT, row, records)
        assert result.success is True
        assert result.company == "太平洋"
        assert result.route_source == "full_match"

    def test_route_dsk_box_match(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(
            row_idx=0,
            email_type=EmailType.DSK,
            container_no="CICU1234567",
        )
        records = [{
            "id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1234567",
            "company": "太平洋", "状态": "正常", "is_deleted": 0,
        }]
        result = route_row(EmailType.DSK, row, records)
        assert result.success is True
        assert result.company == "太平洋"
        assert result.route_source == "box_match"

    def test_route_tracing_multi_company(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(
            row_idx=0,
            email_type=EmailType.TRACING,
            train_no="793",
            container_no=None,
        )
        records = [
            {"id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1234567", "company": "太平洋", "班列号": "WB793", "状态": "正常", "is_deleted": 0},
            {"id": 2, "客户编码": "CQWLJT260810002", "箱号": "CICU1234568", "company": "东盟", "班列号": "WB793", "状态": "正常", "is_deleted": 0},
        ]
        result = route_row(EmailType.TRACING, row, records)
        assert isinstance(result, list)
        assert len(result) == 2
        companies = {r.company for r in result}
        assert "太平洋" in companies
        assert "东盟" in companies


class TestDecide:
    def test_decide_draft_t1(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(row_idx=0, email_type=EmailType.DRAFT, customer_code="CQWLJT260810001", container_no="CICU1234567")
        records = [{"id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1234567", "company": "太平洋", "状态": "正常", "is_deleted": 0}]
        routing = RoutingResult("太平洋", "maoxiaoyang@cqtransit.com", ["to@test.com"], [], "full_match", True)
        decision = decide(EmailType.DRAFT, row, routing, records)
        assert decision.tier == "T1"
        assert decision.action == "forward"

    def test_decide_dsk_t1(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1234567")
        records = [{"id": 1, "客户编码": "CQWLJT260810001", "箱号": "CICU1234567", "company": "太平洋", "状态": "正常", "is_deleted": 0}]
        routing = RoutingResult("太平洋", "maoxiaoyang@cqtransit.com", ["to@test.com"], [], "box_match", True)
        decision = decide(EmailType.DSK, row, routing, records)
        assert decision.tier == "T1"
        assert decision.action == "forward"

    def test_decide_dsk_no_match_alarm(self):
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="NOTEXIST")
        records = []
        routing = RoutingResult("", None, [], [], "no_route", False)
        decision = decide(EmailType.DSK, row, routing, records)
        assert decision.tier == "T0"
        assert decision.action == "alarm"


class TestExtractors:
    def test_draft_extractor_basic(self):
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        msg = MIMEMultipart()
        msg["Subject"] = "运单草单 CQWLJT260810001"
        msg["From"] = "youlia@yxologistics.com"
        msg.attach(MIMEText("test body", "plain"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"test")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="CICU1234567-260810-123456已加密.pdf")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.DRAFT, raw)
        assert len(rows) == 1
        assert rows[0].draft_category == "A"
        assert rows[0].customer_code == "CQWLJT260810001"
        assert rows[0].container_no == "CICU1234567"

    @pytest.mark.skipif(not HAS_XLWT, reason="xlwt not installed")
    def test_waybill_extractor(self):
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        import io
        import xlwt

        wb = xlwt.Workbook()
        ws = wb.add_sheet("Sheet1")
        ws.write(0, 0, "客户编码")
        ws.write(0, 1, "箱号")
        ws.write(0, 2, "运单号")
        ws.write(1, 0, "CQWLJT260810001")
        ws.write(1, 1, "CICU1234567")
        ws.write(1, 2, "RWB123456")
        buf = io.BytesIO()
        wb.save(buf)
        xls_bytes = buf.getvalue()

        msg = MIMEMultipart()
        msg["Subject"] = "运单号下发"
        msg["From"] = "docwbfb@yxologistics.com"
        msg.attach(MIMEText("test", "plain"))

        att = MIMEBase("application", "xls")
        att.set_payload(xls_bytes)
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="waybill.xls")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.WAYBILL, raw)
        assert len(rows) == 1
        assert rows[0].customer_code == "CQWLJT260810001"
        assert rows[0].container_no == "CICU1234567"
        assert "waybill_row" in rows[0].raw_data

    @pytest.mark.skipif(not HAS_XLWT, reason="xlwt not installed")
    def test_tracing_extractor(self):
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        import io
        import xlwt

        wb = xlwt.Workbook()
        ws = wb.add_sheet("Sheet1")
        ws.write(0, 0, "箱号")
        ws.write(0, 1, "目的站")
        ws.write(0, 2, "状态")
        ws.write(1, 0, "CICU1234567")
        ws.write(1, 1, "莫斯科")
        ws.write(1, 2, "在途")
        ws.write(2, 1, "备注")
        buf = io.BytesIO()
        wb.save(buf)
        xls_bytes = buf.getvalue()

        msg = MIMEMultipart()
        msg["Subject"] = "train 793 tracking"
        msg["From"] = "tracing-system@yxologistics.com"
        msg.attach(MIMEText("test", "plain"))

        att = MIMEBase("application", "xls")
        att.set_payload(xls_bytes)
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="tracing.xls")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.TRACING, raw)
        assert len(rows) == 1
        assert rows[0].train_no == "793"
        assert rows[0].container_no == "CICU1234567"

    def test_tracing_malformed_xls_returns_empty(self):
        """畸形附件不抛异常、不卡流水线：返回空行并记日志."""
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        msg = MIMEMultipart()
        msg["Subject"] = "train 793 tracking"
        msg["From"] = "tracing-system@yxologistics.com"
        msg.attach(MIMEText("test", "plain"))

        att = MIMEBase("application", "xls")
        att.set_payload(b"not-an-xls-at-all")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="tracing.xls")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.TRACING, raw)
        assert rows == []

    def test_dsk_extractor_kasa(self):
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        html = """
        <html><body><table>
        <tr><td>CICU1234567</td><td>Code1</td></tr>
        <tr><td>CICU1234568</td><td>Code2</td></tr>
        </table></body></html>
        """

        msg = MIMEMultipart()
        msg["Subject"] = "DSK notification"
        msg["From"] = "kasa@rtsb.de"
        msg.attach(MIMEText(html, "html"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"test")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="CICU1234567.PDF")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.DSK, raw)
        assert len(rows) == 2
        boxes = {r.container_no for r in rows}
        assert "CICU1234567" in boxes
        assert "CICU1234568" in boxes

    def test_atb_extractor(self):
        import email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        msg = MIMEMultipart()
        msg["Subject"] = "ATB for YXO-2026-638 CQWLJT260722001-D CICU1912468"
        msg["From"] = "atb@yxologistics.com"
        msg.attach(MIMEText("test", "plain"))

        att = MIMEBase("application", "pdf")
        att.set_payload(b"test")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="CICU1912468_ATB.pdf")
        msg.attach(att)

        raw = msg.as_bytes()
        rows = extract_email(EmailType.ATB, raw)
        assert len(rows) == 1
        assert rows[0].container_no == "CICU1912468"
        assert rows[0].customer_code_num == "260722001"


def _msg_parts(m):
    """Split a built message into (texts, attachment_filenames)."""
    texts, files = [], []
    for part in m.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disp:
            files.append(part.get_filename())
        elif ctype in ("text/html", "text/plain"):
            payload = part.get_payload(decode=True)
            if payload:
                texts.append(payload.decode("utf-8", errors="replace"))
    return texts, files


def _draft_mail(subject, sender, filenames, body="正文"):
    import email as email_mod
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg.attach(MIMEText(body, "plain"))
    for fn in filenames:
        att = MIMEBase("application", "pdf")
        att.set_payload(b"test")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename=fn)
        msg.attach(att)
    raw = msg.as_bytes()
    return msg, raw


def _seed_draft_nums(code_nums):
    from mailbots_next.config import DRAFT_NUMS_DB_PATH
    conn = sqlite3.connect(DRAFT_NUMS_DB_PATH)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS forwarded_drafts(code_num TEXT PRIMARY KEY)")
        for n in code_nums:
            conn.execute("INSERT OR IGNORE INTO forwarded_drafts(code_num) VALUES (?)", (n,))
        conn.commit()
    finally:
        conn.close()


class TestSplitRules:
    """契约 §10 转发拆分规则."""

    def test_tracing_multi_company_each_gets_full_copy(self):
        """运踪多箱号分属多公司 → 每命中公司各收一份完整副本（内容不裁剪，只扩收件人）."""
        import email as email_mod
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        from mailbots_next.core.act import build_forward_message
        from mailbots_next.core.extract import ExtractedRow

        orig = MIMEMultipart()
        orig["Subject"] = "Tracing train 793"
        orig["From"] = "tracing-system@yxologistics.com"
        orig.attach(MIMEText("track info", "plain"))
        for fn in ("CICU1000001.xls", "report.pdf"):
            att = MIMEBase("application", "octet-stream")
            att.set_payload(b"data-" + fn.encode())
            encoders.encode_base64(att)
            att.add_header("Content-Disposition", "attachment", filename=fn)
            orig.attach(att)
        msg = email_mod.message_from_bytes(orig.as_bytes())

        row = ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no="793")
        records = [
            {"id": 1, "客户编码": "CQWLJT260810001-SPB", "箱号": "CICU1000001", "company": "太平洋", "班列号": "WB793", "状态": "正常", "is_deleted": 0},
            {"id": 2, "客户编码": "CQWLJT260810002-VXN", "箱号": "CICU1000002", "company": "东盟", "班列号": "WB793", "状态": "正常", "is_deleted": 0},
        ]
        res = route_row(EmailType.TRACING, row, records)
        assert isinstance(res, list) and len(res) == 2
        d = decide(EmailType.TRACING, row, res, records)
        assert (d.tier, d.action) == ("TRACE_HIT", "forward")

        built = [build_forward_message(msg, r.to_list, r.cc_list, "sender@t.com") for r in res]
        subjects = {str(m["Subject"]) for m in built}
        assert subjects == {"Tracing train 793"}
        bodies = [_msg_parts(m)[0] for m in built]
        assert bodies[0] == bodies[1] and any("track info" in t for t in bodies[0])
        files = [sorted(_msg_parts(m)[1]) for m in built]
        assert files[0] == files[1] == ["CICU1000001.xls", "report.pdf"]
        assert built[0]["To"] != built[1]["To"]

    def test_dsk_split_row_keeps_own_row_and_attachment(self):
        """DSK 多箱号按行拆分 → 每行仅保留本箱号行 + 随行附件."""
        from mailbots_next.core.act import split_dsk_by_box
        html = (
            "<html><body><table>"
            "<tr><td>CICU1000001</td><td>CodeA</td></tr>"
            "<tr><td>CICU1000002</td><td>CodeB</td></tr>"
            "</table></body></html>"
        )
        atts = [
            {"filename": "CICU1000001.PDF", "payload": b"a", "content_type": "application/pdf"},
            {"filename": "CICU1000002.PDF", "payload": b"b", "content_type": "application/pdf"},
            {"filename": "readme.txt", "payload": b"r", "content_type": "text/plain"},
        ]
        rows = [("CICU1000001", "CodeA"), ("CICU1000002", "CodeB")]
        cropped, matched = split_dsk_by_box(None, html, atts, rows, "CICU1000001")
        assert "CICU1000001" in cropped and "CICU1000002" not in cropped
        assert [a["filename"] for a in matched] == ["CICU1000001.PDF"]

    def test_dsk_split_message_assembly_keeps_subject_body_unrelated(self):
        """拆分邮件：标题原样（仅动作层追加客编）+ 正文/其余附件原样带入每封."""
        import email as email_mod
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders
        from mailbots_next.core.act import (
            build_forward_message, split_dsk_by_box, build_dsk_subject,
        )
        orig = MIMEMultipart()
        orig["Subject"] = "DSK notice"
        orig["From"] = "kasa@rtsb.de"
        orig.attach(MIMEText("cover letter", "plain"))
        att = MIMEBase("application", "pdf")
        att.set_payload(b"x")
        encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment", filename="readme.txt")
        orig.attach(att)
        msg = email_mod.message_from_bytes(orig.as_bytes())

        html = "<html><body><table><tr><td>CICU1000001</td><td>CodeA</td></tr></table></body></html>"
        atts = [{"filename": "readme.txt", "payload": b"x", "content_type": "application/pdf"}]
        cropped, matched = split_dsk_by_box(None, html, atts, [("CICU1000001", "CodeA")], "CICU1000001")
        out = build_forward_message(
            msg, ["a@x.com"], [], "sender@t.com",
            html_override=cropped, attachments_override=matched + atts,
        )
        assert str(out["Subject"]) == "DSK notice"
        texts, files = _msg_parts(out)
        assert any("CICU1000001" in t for t in texts)
        assert "readme.txt" in files
        assert build_dsk_subject("DSK notice", "CICU1000001", "CQWLJT260810001") == \
            "【CICU1000001】DSK notice - CQWLJT260810001"

    def test_waybill_split_groups_by_company_with_own_rows(self):
        """运单号按公司拆分：每收件组只见自己行，标题原样."""
        import email as email_mod
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from mailbots_next.core.act import split_waybill_by_company
        orig = MIMEMultipart()
        orig["Subject"] = "运单号下发"
        orig.attach(MIMEText("body", "plain"))
        msg = email_mod.message_from_bytes(orig.as_bytes())
        rows = [
            {"客户编码": "CQWLJT260810001", "箱号": "CICU1000001", "运单号": "R1", "company": "太平洋"},
            {"客户编码": "CQWLJT260810002", "箱号": "CICU1000002", "运单号": "R2", "company": "东盟"},
        ]
        routes = {"太平洋": (["tp@test.com"], []), "东盟": (["dm@test.com"], [])}
        groups = split_waybill_by_company(msg, rows, routes)
        assert len(groups) == 2
        by_company = {g["company"]: g for g in groups}
        assert str(by_company["太平洋"]["msg"]["Subject"]) == "运单号下发"
        texts, _ = _msg_parts(by_company["太平洋"]["msg"])
        assert any("CICU1000001" in t for t in texts)
        assert not any("CICU1000002" in t for t in texts)

    def test_tracing_zero_hit_logs_info_only(self):
        """运踪箱号无一命中 → 仅 INFO，不转发、不报警、不进错误队列."""
        from unittest.mock import Mock, patch
        from mailbots_next.core.extract import ExtractedRow
        from mailbots_next.core import get_connection
        import mailbots_next.serve as serve_mod

        processor = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
        processor.notifier = Mock()
        row = ExtractedRow(row_idx=0, email_type=EmailType.TRACING, train_no="999")
        routing = RoutingResult("", None, [], [], "no_company_match", False)
        with patch.object(serve_mod, "_log") as mock_log, \
             patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            processor._process_routing_result(
                routing, row, "tracing", "zero-msg-1", "0:?", b"raw",
                "a@x.com", "INBOX", 7, "s", "subj", "",
            )
        mock_log.info.assert_called_once()
        assert "zero" in mock_log.info.call_args[0][0].lower()
        mock_smtp.assert_not_called()
        processor.notifier.send_alarm.assert_not_called()
        processor.notifier.send_config_missing.assert_not_called()
        conn = get_connection()
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM error_queue WHERE message_id=?", ("zero-msg-1",)
            ).fetchone()["c"]
        finally:
            conn.close()
        assert n == 0

    def test_missing_recipients_goes_E2(self):
        """命中公司缺收件人配置 → E2（日志 + 告警），不静默跳过."""
        from unittest.mock import Mock, patch
        from mailbots_next.core.extract import ExtractedRow
        from mailbots_next.core import get_connection, get_pending_errors
        import mailbots_next.serve as serve_mod

        processor = serve_mod.MailProcessor.__new__(serve_mod.MailProcessor)
        processor.notifier = Mock()
        row = ExtractedRow(
            row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001",
        )
        routing = RoutingResult("太平洋", None, [], [], "no_route", False)
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            processor._process_routing_result(
                routing, row, "dsk", "e2-msg-1", "0:CICU1000001", b"raw",
                "a@x.com", "INBOX", 7, "s", "subj", "",
            )
        processor.notifier.send_config_missing.assert_called_once()
        from mailbots_next.config.settings import OPS_OWNER_EMAIL
        args = processor.notifier.send_config_missing.call_args[0]
        assert args[0] == OPS_OWNER_EMAIL and "太平洋" in args[2]
        mock_smtp.assert_not_called()
        pending = [e for e in get_pending_errors(100) if e["message_id"] == "e2-msg-1"]
        assert len(pending) == 1


def _tier_row(code=None, box=None, category=None):
    from mailbots_next.core.extract import ExtractedRow
    return ExtractedRow(
        row_idx=0, email_type=EmailType.DRAFT,
        customer_code=code, container_no=box, draft_category=category,
    )


def _tier_rec(code, box, company="太平洋"):
    return {"id": 1, "客户编码": code, "箱号": box, "company": company,
            "状态": "正常", "is_deleted": 0}


_NO_ROUTE = RoutingResult("", None, [], [], "no_route", False)


class TestEightTiers:
    """八档判定 T0/T2/T3/T4/T5/T6/T7 命中条件与处置."""

    def test_decide_T0_no_code_no_box_alarm(self):
        d = decide(EmailType.DRAFT, _tier_row(), _NO_ROUTE, [])
        assert (d.tier, d.action) == ("T0", "alarm")

    def test_decide_T2_same_stem_same_box_diff_suffix_pending(self):
        row = _tier_row("CQWLJT260810001-VXN", "CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T2", "pending")

    def test_decide_T7_stem_spans_two_suffixes_pending(self):
        row = _tier_row("CQWLJT260810001-XXX", None)
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001"),
                _tier_rec("CQWLJT260810001-VXN", "CICU1000002", "东盟")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T7", "pending")

    def test_decide_T3_stem_hit_box_mismatch_pending(self):
        row = _tier_row("CQWLJT260810001-VXN", "CICU9999999")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T3", "pending")

    def test_decide_T4_stem_absent_alarm(self):
        row = _tier_row("CQWLJT260899999-AAA", "CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T4", "alarm")

    def test_decide_T5_unique_box_no_code_alarm(self):
        row = _tier_row(None, "CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T5", "alarm")

    def test_decide_T6_reused_box_pending(self):
        row = _tier_row(None, "CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001"),
                _tier_rec("CQWLJT260810002-VXN", "CICU1000001", "东盟")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T6", "pending")

    def test_decide_dsk_reused_box_alarms_T6(self):
        """DSK 甲方案偏差锁定：箱号复用≥2 → T6 但处置为报警."""
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(row_idx=0, email_type=EmailType.DSK, container_no="CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001"),
                _tier_rec("CQWLJT260810002-VXN", "CICU1000001", "东盟")]
        d = decide(EmailType.DSK, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T6", "alarm")

    def test_decide_waybill_T2_shared_engine(self):
        """运单号共享草单八档引擎：同序号同箱号异后缀 → T2 待办."""
        from mailbots_next.core.extract import ExtractedRow
        row = ExtractedRow(row_idx=0, email_type=EmailType.WAYBILL,
                           customer_code="CQWLJT260810001-VXN", container_no="CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.WAYBILL, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T2", "pending")

    def test_decide_T1_exact_wins_over_T7(self):
        """精确命中优先：同序号多后缀并存时，精确客编仍走 T1."""
        row = _tier_row("CQWLJT260810001-SPB", "CICU1000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001"),
                _tier_rec("CQWLJT260810001-VXN", "CICU1000002", "东盟")]
        ok = RoutingResult("太平洋", "m@x.com", ["t@x.com"], [], "full_match", True)
        d = decide(EmailType.DRAFT, row, ok, recs)
        assert (d.tier, d.action) == ("T1", "forward")


class TestScopeIron:
    """解析范围铁律：WHERE 状态<>'退舱' AND is_deleted=0."""

    def test_scope_filter_excludes_cancelled_and_deleted(self, tmp_path):
        import mailbots_next.core.store as store_mod
        db = str(tmp_path / "scope_test.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE records (id INTEGER PRIMARY KEY, 客户编码 TEXT, 箱号 TEXT, "
                "班列号 TEXT, 目的站 TEXT, 开票子公司名称 TEXT, 本地货源公司 TEXT, "
                "状态 TEXT, is_deleted INTEGER, dsk TEXT, ATB TEXT)"
            )
            conn.executemany(
                "INSERT INTO records (客户编码, 箱号, 班列号, 目的站, 开票子公司名称, "
                "本地货源公司, 状态, is_deleted, dsk, ATB) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    ("CQWLJT260810001-SPB", "CICU1000001", "WB1", "站", "太平洋", "", "正常", 0, "", ""),
                    ("CQWLJT260810002-SPB", "CICU1000002", "WB1", "站", "太平洋", "", "退舱", 0, "", ""),
                    ("CQWLJT260810003-SPB", "CICU1000003", "WB1", "站", "太平洋", "", "正常", 1, "", ""),
                    ("CQWLJT260810004-SPB", "CICU1000004", "WB1", "站", "太平洋", "", "正常", None, "", ""),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        try:
            old = store_mod.YXO_DB_PATH
            store_mod.YXO_DB_PATH = db
            rows = store_mod.load_records()
        finally:
            store_mod.YXO_DB_PATH = old
            Path(db).unlink(missing_ok=True)
        codes = {r["客户编码"] for r in rows}
        assert codes == {"CQWLJT260810001-SPB", "CQWLJT260810004-SPB"}

    def test_scope_filter_cancelled_stem_reports_T4(self):
        """退舱记录被过滤后，命中退舱客编走 T4 报警而非 T1."""
        row = _tier_row("CQWLJT260800001-SPB", "CICU9000001")
        recs = [_tier_rec("CQWLJT260810001-SPB", "CICU1000001")]
        d = decide(EmailType.DRAFT, row, _NO_ROUTE, recs)
        assert (d.tier, d.action) == ("T4", "alarm")


class TestDraftCategories:
    """草单 A/B/C1/C2/其他 判别与动作后果."""

    def test_draft_category_A_new_draft_goes_to_tiers(self):
        _, raw = _draft_mail(
            "运单草单 CQWLJT260810001", "youlia@yxologistics.com",
            ["CICU1000001-260810-123456已加密.pdf"],
        )
        rows = extract_email(EmailType.DRAFT, raw)
        assert rows[0].draft_category == "A"
        recs = [_tier_rec("CQWLJT260810001", "CICU1000001")]
        ok = RoutingResult("太平洋", "m@x.com", ["t@x.com"], [], "full_match", True)
        d = decide(EmailType.DRAFT, rows[0], ok, recs)
        assert (d.tier, d.action) == ("T1", "forward")

    def test_draft_category_B_update_detected(self):
        _seed_draft_nums(["260810001"])
        _, raw = _draft_mail(
            "运单草单 CQWLJT260810001", "youlia@yxologistics.com",
            ["CICU1000001-260810-123456已加密.pdf"],
        )
        rows = extract_email(EmailType.DRAFT, raw)
        assert rows[0].draft_category == "B"

    def test_draft_B_forward_has_no_extra_note_and_sends_update_notice(self, monkeypatch):
        """B 转发不附加文字（无"作废"），企微发 N3 草单更新而非 N4."""
        from unittest.mock import Mock, patch
        from mailbots_next.serve import MailProcessor
        from mailbots_next.config import snapshot
        monkeypatch.setenv("MAILBOT_MODE", "live")
        _seed_draft_nums(["260810001"])
        subject = "运单草单 CQWLJT260810001"
        _, raw = _draft_mail(
            subject, "youlia@yxologistics.com",
            ["CICU1000001-260810-123456已加密.pdf"],
        )
        proc = MailProcessor(snapshot())
        proc.records = [_tier_rec("CQWLJT260810001", "CICU1000001")]
        proc.accounts = {"t@t.com": "pw"}
        proc.notifier = Mock()
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": True, "refused": {}}
            proc.process_email("t@t.com", "草单", "b-msg-1", 1,
                               subject, "youlia@yxologistics.com", "", raw)
        mock_smtp.assert_called_once()
        sent_msg = mock_smtp.call_args[0][0]
        assert str(sent_msg["Subject"]) == subject
        assert "作废" not in sent_msg.as_string()
        proc.notifier.send_draft_update.assert_called_once()
        proc.notifier.send_forwarded.assert_not_called()

    def test_draft_A_sends_forward_notice(self, monkeypatch):
        from unittest.mock import Mock, patch
        from mailbots_next.serve import MailProcessor
        from mailbots_next.config import snapshot
        monkeypatch.setenv("MAILBOT_MODE", "live")
        subject = "运单草单 CQWLJT260810001"
        _, raw = _draft_mail(
            subject, "youlia@yxologistics.com",
            ["CICU1000001-260810-123456已加密.pdf"],
        )
        proc = MailProcessor(snapshot())
        proc.records = [_tier_rec("CQWLJT260810001", "CICU1000001")]
        proc.accounts = {"t@t.com": "pw"}
        proc.notifier = Mock()
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp:
            mock_smtp.return_value = {"success": True, "refused": {}}
            proc.process_email("t@t.com", "草单", "a-msg-1", 1,
                               subject, "youlia@yxologistics.com", "", raw)
        mock_smtp.assert_called_once()
        proc.notifier.send_forwarded.assert_called_once()
        proc.notifier.send_draft_update.assert_not_called()

    def test_draft_C1_upstream_feedback_goes_to_tiers(self):
        _, raw = _draft_mail(
            "关于CQWLJT260810001的反馈", "chenlong@yxologistics.com",
            ["report.pdf"], body="请帮忙看一下",
        )
        rows = extract_email(EmailType.DRAFT, raw)
        assert rows[0].draft_category == "C1"
        recs = [_tier_rec("CQWLJT260810001", None)]
        ok = RoutingResult("太平洋", "m@x.com", ["t@x.com"], [], "full_match", True)
        d = decide(EmailType.DRAFT, rows[0], ok, recs)
        assert (d.tier, d.action) == ("T1", "forward")

    def test_draft_C2_stays_manual_no_queue_no_notify(self):
        """C2 留人工：skip，不转发、不进错误队列、不推送企微（INFO 可审计）.

        C2 可达形态：附件名含"箱号"（命中类型识别）但不符合草单附件
        严格命名（ENC_PDF_RE / BOX_PDF_RE），即业务人员普通邮件。
        """
        from unittest.mock import Mock, patch
        from mailbots_next.serve import MailProcessor
        from mailbots_next.config import snapshot
        from mailbots_next.core import get_pending_errors
        subject = "Re: 确认"
        _, raw = _draft_mail(
            subject, "customer@gmail.com", ["箱号说明.txt"], body="收到",
        )
        rows = extract_email(EmailType.DRAFT, raw)
        assert rows[0].draft_category == "C2"
        proc = MailProcessor(snapshot())
        proc.records = [_tier_rec("CQWLJT260810001", "CICU1000001")]
        proc.accounts = {"t@t.com": "pw"}
        proc.notifier = Mock()
        with patch("mailbots_next.core.act.send_smtp") as mock_smtp, \
             patch("mailbots_next.core.log.EmailLogger.log_manual_category") as mock_manual:
            proc.process_email("t@t.com", "草单", "c2-msg-1", 1,
                               subject, "customer@gmail.com", "", raw)
        mock_smtp.assert_not_called()
        proc.notifier.send_forwarded.assert_not_called()
        proc.notifier.send_draft_update.assert_not_called()
        proc.notifier.send_alarm.assert_not_called()
        proc.notifier.send_pending.assert_not_called()
        mock_manual.assert_called_once()
        assert mock_manual.call_args[0][1] == "C2"
        assert [e for e in get_pending_errors(100)
                if e["message_id"] == "c2-msg-1"] == []

    def test_draft_OTHER_unknown_category_skips(self):
        row = _tier_row("CQWLJT260810001", "CICU1000001", category="OTHER")
        recs = [_tier_rec("CQWLJT260810001", "CICU1000001")]
        ok = RoutingResult("太平洋", "m@x.com", ["t@x.com"], [], "full_match", True)
        d = decide(EmailType.DRAFT, row, ok, recs)
        assert (d.tier, d.action) == ("MANUAL", "skip")