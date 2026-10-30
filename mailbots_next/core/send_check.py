"""发送后自检：两路取证 + 分级判定（设计见 02_SPEC §4.2 / §4.3）。

两路互相独立：
  路 A = 本封发信账号的「已发送」副本  → 证明「自己邮箱存档了」
  路 B = QQ 旁路邮箱收到的副本        → 证明「真的跨域投递出去了」
两路都不能证明客户看到了，那一层只能靠 NDU/NDR（bounce.py）。

判定与取数分离：IMAP 客户端通过 `sent_probe` / `qq_probe` 注入，
生产路径不注入时用本模块的 `collect_sent` / `collect_qq` 默认实现。
"""
import email
import imaplib
import json
import re
from datetime import datetime, timezone

from ..config import OPS_OWNER_EMAIL, settings
from .log import get_logger
from .notify import get_notifier, increment_counter
from .store import get_owner_email, get_pending_checks, mark_check_result

_log = get_logger(__name__)

RESULT_AB = "AB"            # 两路都有：正常，不告警
RESULT_A_ONLY = "A_ONLY"    # 存档有、外部无
RESULT_B_ONLY = "B_ONLY"    # 外部有、存档无
RESULT_NONE = "NONE"        # 两路都无
RESULT_SKIP_ERR = "SKIP_ERR"  # 无法判定（写回一个非空 checked_at 免得反复重试）

# 匹配键（写头处见 act.py 的 build_forward_message / _forward_split）。
FID_HEADER = "X-YXO-Forward-Id"

# RFC 3501 月份缩写。不用 strftime("%b")：它随 locale 变化，中文 Windows 会给出
# 「10月」这种非 ASCII 结果，IMAP SEARCH 直接失败。
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_STAT_KEY = {
    RESULT_AB: "ab",
    RESULT_A_ONLY: "a_only",
    RESULT_B_ONLY: "b_only",
    RESULT_NONE: "none",
}


def classify(in_a: bool, in_b: bool) -> str:
    """纯函数，四种组合，别加额外分支。"""
    if in_a and in_b:
        return RESULT_AB
    if in_a:
        return RESULT_A_ONLY
    if in_b:
        return RESULT_B_ONLY
    return RESULT_NONE


def parse_fid(raw_header_text) -> str:
    """从一段原始头文本里取 X-YXO-Forward-Id；没有就返回 None。

    只做 strip（字符串精确相等，SPEC §4.5）；不做任何其它归一化。
    """
    if not raw_header_text:
        return None
    try:
        msg = email.message_from_string(raw_header_text)
    except Exception:
        return None
    val = msg.get(FID_HEADER)
    return val.strip() if val else None


def _decode(raw) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _since_ddmonyyyy(now=None) -> str:
    """路 A 的 SINCE 参数：UTC 当天，`%d-%b-%Y`（例 29-Oct-2026）。

    RFC 3501 允许 1~2 位日期，这里统一两位，不要一半一半。
    """
    dt = now or datetime.now(timezone.utc)
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year:04d}"


def collect_sent(account, password, since_ddmonyyyy) -> set:
    """路 A：查该账号「已发送」，返回 fid 集合。

    取数写法照 02_input/imap_capture_reference.py 的 collect_aliyun_sent()：
    SEARCH SINCE（阿里有效）→ 一次批量 FETCH (BODY.PEEK[HEADER]) 全头 → 本地解析。
    ⚠️ 不用 HEADER.FIELDS（阿里静默忽略自定义头）、不用 SEARCH HEADER（阿里假阴）。
    ⚠️ 不要改成「取尾部 N 封」：阿里已发送的序号顺序不等于时间序，会漏。
    连接/登录/取数任一失败都向上抛，由 run_once 记入 failed_accounts。
    """
    fids = set()
    conn = imaplib.IMAP4_SSL(settings.IMAP_SERVER, settings.IMAP_PORT)
    try:
        conn.login(account, password)
        typ, _ = conn.select(settings.SENT_FOLDER_UTF7, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"select {settings.SENT_FOLDER_UTF7!r} failed")
        typ, data = conn.search(None, f"(SINCE {since_ddmonyyyy})")
        if typ != "OK":
            raise RuntimeError(f"search failed: {data}")
        seqs = data[0].split() if data and data[0] else []
        if not seqs:
            return fids
        # 一次 FETCH 批量拉全头（逐封会慢一个数量级）
        typ, res = conn.fetch(",".join(s.decode() for s in seqs),
                              "(BODY.PEEK[HEADER])")
        if typ != "OK":
            raise RuntimeError(f"fetch failed: {res}")
        for item in (res or []):
            if isinstance(item, tuple):
                fid = parse_fid(_decode(item[1]))
                if fid:
                    fids.add(fid)
        return fids
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _qq_last_segment(folder_name: str) -> str:
    return folder_name.rsplit("/", 1)[-1].strip().lower()


def list_qq_folders(conn) -> list:
    """动态枚举要查的 QQ 文件夹：INBOX + 「其他文件夹」下全部子夹。

    🔴 不写死文件夹清单：洋在 QQ 侧随时会新建归档夹，写死会在那天集体假缺失。
    排除 QQ_VERIFY_EXCLUDE（前两个实测 EXAMINE 会报 [CLIENTBUG]），
    并跳过 QQ_VERIFY_SKIP_SUBFOLDERS（默认 tracing：不纳入核对，只是省时间）。
    """
    typ, folders = conn.list()
    if typ != "OK":
        raise RuntimeError(f"list failed: {folders}")
    out = []
    for raw in folders or []:
        s = _decode(raw)
        # 形如 (\HasNoChildren) "/" "&UXZO1mWHTvZZOQ-/DSK"
        name = s.split(' "/" ')[-1].strip('"')
        if name == s.strip():
            quoted = re.findall(r'"([^"]*)"', s)
            name = quoted[-1] if quoted else ""
        if not name:
            continue
        if name in settings.QQ_VERIFY_EXCLUDE:
            continue
        if _qq_last_segment(name) in {x.lower() for x in settings.QQ_VERIFY_SKIP_SUBFOLDERS}:
            continue
        if name == "INBOX" or name.startswith(settings.QQ_VERIFY_OTHER_ROOT + "/"):
            out.append(name)
    return out


def collect_qq_folder(conn, folder, tail_n) -> set:
    """QQ：取序号尾部 tail_n 封 → 全头 → 收集 fid 集合。

    ⚠️ 腾讯 SEARCH SINCE 完全失效（返回全部），只能「取序号尾部 K 封」。
    序号正序，最大 = 最新（实测确证）。
    打开/取数失败向上抛，由 collect_qq 记入 failed_folders。
    """
    fids = set()
    typ, data = conn.select(folder, readonly=True)
    if typ != "OK":
        raise RuntimeError(f"select {folder!r} failed")
    total = int(data[0]) if data and data[0] else 0
    if total == 0:
        return fids
    start = max(1, total - int(tail_n) + 1)
    seqs = [str(i) for i in range(start, total + 1)]
    typ, res = conn.fetch(",".join(seqs), "(BODY.PEEK[HEADER])")
    if typ != "OK":
        raise RuntimeError(f"fetch {folder!r} failed")
    for item in (res or []):
        if isinstance(item, tuple):
            fid = parse_fid(_decode(item[1]))
            if fid:
                fids.add(fid)
    return fids


def collect_qq(tail_n) -> tuple:
    """路 B：登录 QQ，动态枚举文件夹后逐夹取尾部 K 封。

    返回 (命中的 fid 集合, 失败的文件夹名列表) —— 两个都要返回，
    run_once 要把 failed_folders 带出去。

    失败语义分两级（照抄参考实现的「单夹打警告继续」会做出假缺失）：
      登录/连接失败、INBOX 失败、所有目标夹都失败 ⇒ 抛异常（run_once 整批搁置）
      仅非 INBOX 的单个夹失败 ⇒ 记入 failed_folders 后继续（别的夹可能也有同一个 fid）
    """
    user, pwd = _qq_credentials()
    conn = imaplib.IMAP4_SSL(settings.QQ_VERIFY_IMAP_SERVER, settings.QQ_VERIFY_IMAP_PORT)
    try:
        conn.login(user, pwd)
        folders = list_qq_folders(conn)
        if not folders:
            raise RuntimeError("QQ 侧枚举到的目标文件夹为空")
        fids, failed = set(), []
        ok_folders = 0
        for folder in folders:
            try:
                fids |= collect_qq_folder(conn, folder, tail_n)
                ok_folders += 1
            except Exception as e:
                failed.append(folder)
                _log.warning(f"send_check: QQ folder {folder!r} failed: "
                             f"{type(e).__name__}: {e}")
        if "INBOX" in failed:
            raise RuntimeError("QQ INBOX 取数失败，本轮路 B 证据不可信")
        if ok_folders == 0:
            raise RuntimeError("QQ 所有目标文件夹取数都失败")
        return fids, failed
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _sent_password(account) -> str:
    """路 A 密码：与 SMTP 发信、IMAP 收信完全同一套（ingest.py 就是这么取的）。
    不需要任何新凭据、不要用临时探测密码。"""
    from ..config import get_accounts
    return get_accounts().get(account, "")


def _qq_credentials() -> tuple:
    """路 B 凭据：环境变量优先，回退 secrets.json。

    settings.py 里 QQ_VERIFY_* 只读 env 是对的 —— 那里 import secrets 会循环导入
    （config/secrets.py 本身 from .settings import SECRETS_PATH）。回退只能放这一层。
    取不到就在这里直接抛：拿空密码去 login() 会得到一个误导性的认证错误。
    """
    from ..config.secrets import load_secrets
    secrets = load_secrets() or {}
    user = settings.QQ_VERIFY_USER or secrets.get("QQ_VERIFY_USER", "")
    pwd = settings.QQ_VERIFY_PASSWORD or secrets.get("QQ_VERIFY_PASSWORD", "")
    if not user or not pwd:
        raise RuntimeError("QQ_VERIFY_USER / QQ_VERIFY_PASSWORD 未配置"
                           "（env 与 secrets.json 都为空）")
    return user, pwd


def _recipients_for(result, company) -> list:
    """告警收件人（SPEC §12-①）。

    NONE  → 公司负责人 ∪ 运维负责人，先滤掉 None/空串再去重
            （get_owner_email 查不到公司时返回 None，直接传给 notify 会静默失败）
    其余  → 只有运维负责人
    """
    if result != RESULT_NONE:
        return [OPS_OWNER_EMAIL]
    owner = get_owner_email(company)
    return sorted({r for r in (owner, OPS_OWNER_EMAIL) if r})


def _fmt_list(raw) -> str:
    try:
        addrs = json.loads(raw or "[]")
    except Exception:
        return str(raw or "")
    return ", ".join(str(a) for a in addrs) if isinstance(addrs, list) else str(addrs)


def _mail_detail(result, cand, missing: str) -> str:
    return (
        f"公司: {cand.get('company') or '(未知)'}\n"
        f"主题: {cand.get('subject') or ''}\n"
        f"收件人: {_fmt_list(cand.get('to_list'))}\n"
        f"发信账号: {cand.get('sent_by') or ''}\n"
        f"forward_id: {cand.get('forward_id')}\n"
        f"缺失: {missing}\n"
        f"发送时间(UTC): {cand.get('sent_at') or ''}\n"
        f"说明: {_HINT_BY_RESULT.get(result, '')}"
    )


_HINT_BY_RESULT = {
    RESULT_NONE: "SMTP 返回成功，但「已发送」与 QQ 旁路两侧都没有副本；"
                 "可能是延迟（稍后可能补上），也可能是中继丢弃/策略拦截。",
    RESULT_A_ONLY: "本账号「已发送」有副本，QQ 旁路未见 ⇒ 外部投递未证实；"
                   "常见原因是 QQ 侧归档延迟或该公司 cc 里没有 QQ 邮箱。",
    RESULT_B_ONLY: "QQ 旁路已收到，但本账号「已发送」没有副本 ⇒ 服务端未存档。",
}

_MISSING_BY_RESULT = {
    RESULT_NONE: "两路都没有",
    RESULT_A_ONLY: "缺 QQ 旁路（外部投递未证实）",
    RESULT_B_ONLY: "缺「已发送」存档",
}


def _send_alert(notifier, recipients, detail) -> bool:
    fn = getattr(notifier, "send_send_check", None)
    if fn is None:                                   # 兼容只有 notify() 的实现
        return notifier.notify("send_check", recipients, f"📮 发送自检\n{detail}")
    return fn(recipients, detail)


def _new_stats(n) -> dict:
    return {"candidates": n, "checked": 0, "ab": 0, "a_only": 0, "b_only": 0,
            "none": 0, "skip_err": 0, "alerted": 0, "deferred": False,
            "failed_accounts": [], "failed_folders": []}


def run_once(candidates=None, sent_probe=None, qq_probe=None, notifier=None) -> dict:
    """跑一轮自检。返回统计 dict。

    candidates 不传 ⇒ 走 get_pending_checks（DB 驱动，天然去重）。
    传了就按入参核，不再自己过滤（测试 6b 覆盖这一点）。
    """
    if candidates is None:
        candidates = get_pending_checks(settings.SEND_CHECK_DELAY_SEC,
                                        settings.SEND_CHECK_BATCH_MAX)
    # 空列表 ⇒ 一个 IMAP 连接都不建（绝大多数 sweep 属于这种）
    if not candidates:
        return {"candidates": 0}

    sent_probe = sent_probe or collect_sent
    qq_probe = qq_probe or collect_qq
    notifier = notifier or get_notifier()

    stats = _new_stats(len(candidates))

    # ① 空 sent_by 的行直接判 SKIP_ERR：不是 IMAP 故障，不能混进 send_check_failed
    pending = []
    for cand in candidates:
        sender = (cand.get("sent_by") or "").strip()
        if not sender:
            mark_check_result(cand.get("forward_id"), RESULT_SKIP_ERR)
            increment_counter("send_check_skip_no_sender")
            stats["skip_err"] += 1
            continue
        pending.append(cand)
    if not pending:
        return stats

    # ② 路 A：按发信账号分组，一个账号失败只影响该账号名下的候选
    by_account = {}
    for cand in pending:
        by_account.setdefault(cand["sent_by"], []).append(cand)
    since = _since_ddmonyyyy()
    sent_sets, failed_accounts = {}, []
    for account in by_account:
        pwd = _sent_password(account)
        if not pwd:
            # 空密码 ⇒ 视为该账号登录失败，不要拿去 login()（会得到误导性的认证错误）
            failed_accounts.append(account)
            _log.warning(f"send_check: no IMAP password for {account}, deferred")
            continue
        try:
            sent_sets[account] = set(sent_probe(account, pwd, since) or ())
        except Exception as e:
            failed_accounts.append(account)
            _log.warning(f"send_check: sent probe failed for {account}: "
                         f"{type(e).__name__}: {e}")
    stats["failed_accounts"] = list(failed_accounts)

    # ③ 路 B：一次取完。整路失败 ⇒ 整批搁置（只有路 A 会判出 A_ONLY 的假缺失）
    try:
        got = qq_probe(settings.SEND_CHECK_TAIL_N)
        qq_fids, failed_folders = got if isinstance(got, tuple) else (got, [])
        qq_fids = set(qq_fids or ())
        failed_folders = list(failed_folders or ())
    except Exception as e:
        _log.warning(f"send_check: QQ probe failed, batch deferred: "
                     f"{type(e).__name__}: {e}")
        increment_counter("send_check_failed")
        stats["deferred"] = True
        return stats
    stats["failed_folders"] = failed_folders

    # ④ 逐封判定 + 落库（无论正常与否都写，天然去重）
    judged = []
    for cand in pending:
        account = cand["sent_by"]
        if account in failed_accounts:
            continue                                 # 该账号本轮无证据，下轮再核
        in_a = cand["forward_id"] in sent_sets.get(account, set())
        in_b = cand["forward_id"] in qq_fids
        result = classify(in_a, in_b)
        mark_check_result(cand["forward_id"], result)
        stats["checked"] += 1
        stats[_STAT_KEY[result]] += 1
        if result != RESULT_AB:
            judged.append((result, cand))

    # ⑤ 告警（SPEC §12）：NONE / B_ONLY 逐封；A_ONLY 同公司 ≥2 封合并成一条
    alerted = 0
    for result, cand in judged:
        if result != RESULT_A_ONLY:
            alerted += _alert_one(notifier, result, cand)
    a_only_by_company = {}
    for result, cand in judged:
        if result == RESULT_A_ONLY:
            a_only_by_company.setdefault((cand.get("company") or ""), []).append(cand)
    for company, rows in a_only_by_company.items():
        if len(rows) >= 2:
            detail = (
                f"公司: {company or '(未知)'}\n"
                f"本轮该公司 {len(rows)} 封未见外部投递，QQ 副本链路可能已断"
                f"（是否把 3841559246@qq.com 从 cc 里删了？）\n"
                f"forward_id 列表: " + ", ".join(str(r.get("forward_id")) for r in rows) +
                f"\n发信账号: " + ", ".join(sorted({str(r.get("sent_by")) for r in rows})) +
                f"\n说明: {_HINT_BY_RESULT[RESULT_A_ONLY]}"
            )
            alerted += _alert(notifier, [OPS_OWNER_EMAIL], detail)
        else:
            alerted += _alert_one(notifier, RESULT_A_ONLY, rows[0])

    stats["alerted"] = alerted
    if alerted:
        increment_counter("send_check_alerted", alerted)
    failed_n = len(failed_accounts) + len(failed_folders)
    if failed_n:
        increment_counter("send_check_failed", failed_n)
    return stats


def _alert(notifier, recipients, detail) -> int:
    """发一条告警。返回实际发出的条数（0/1）。"""
    try:
        ok = _send_alert(notifier, recipients, detail)
    except Exception as e:
        _log.error(f"send_check: alert failed: {type(e).__name__}: {e}")
        return 0
    return 1 if ok else 0


def _alert_one(notifier, result, cand) -> int:
    """单封告警。收件人按 SPEC §12-① 分级。"""
    return _alert(notifier, _recipients_for(result, cand.get("company")),
                  _mail_detail(result, cand, _MISSING_BY_RESULT[result]))
