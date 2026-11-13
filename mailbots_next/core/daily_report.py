# -*- coding: utf-8 -*-
"""每日一封汇总邮件（任务 16）。

文本规范唯一真相源：任务包 `06_EMAIL-DESIGN.md` —— 五节结构
（一句话 → 转发概览 → 待办与异常 → 发送与归档自检 → 转发明细）。
**不要**把新内容追加在旧 `build_admin_body()` 输出后面：旧函数是取数来源，
不是邮件骨架。

发送规则（写死，洋已拍板）：
  · 每天恰好 1 封，12:30 —— 等 12:00 的归档核对跑完，数据才齐；
  · 收件人只有洋（`OPS_OWNER_EMAIL`），同事不再收个人版日报；
  · 当日 0 转发 0 异常也发一封短报（「每天一封」要可预期）。

日期口径（极易混，写死见任务书 2.3）：
  · 转发明细 = **昨天**（`forward_log.day` / `tracing_log.log_date`）
  · 09:00 digest 计数 / 12:00 归档 stats / 未分类 / 配置缺失 / 待办 / 程序错误
    = **当天**

数据来源（6 类，逐项落位）：
  ① 转发日报总表（按负责人分组 + 逐条明细）  → 第二、五节
  ② 企微 09:00 digest 的全部计数             → 第四节
  ③ 归档核对 stats（核过/已删/失败/补记/终止）→ 第四节
  ④ 未分类 / 配置缺失汇总                    → 第三节
  ⑤ 待办 pending 汇总                        → 第三节
  ⑥ 程序错误（若有）                          → 第三节

「票」的口径：**实际发出的一封转发邮件算 1 票**。运踪按公司副本各计 1 票
（运踪整封副本 × 命中公司数，本来就是一封一封发出去的）。
"""
import json
import os
import smtplib
import sqlite3
import threading
import unicodedata
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import (
    ARCHIVE_STATS_PATH,
    BOT_CONFIG_DB_PATH,
    DAILY_DIGEST_PATH,
    DAILY_REPORT_HOUR,
    DAILY_REPORT_MINUTE,
    DAILY_REPORT_SENT_PATH,
    LEGACY_FORWARD_LOG_PATH,
    OPS_OWNER_EMAIL,
    EmailType,
    ROBOT_LABELS,
    SMTP_PORT,
    SMTP_SERVER,
    YXO_DB_PATH,
)
from .log import get_logger
from .notify import get_counters_snapshot, get_digest_snapshot, get_issues

_log = get_logger(__name__)

# 与邮件正文第二节固定列顺序一致（0 票也要列出来，见设计稿）。
# 任务 18：改为从 ROBOT_LABELS 派生（中文名只有一处定义），但**顺序写死**，
# 必须仍等于 ("运踪", "草单", "运单号", "ATB", "DSK")。
ROBOT_ORDER = tuple(ROBOT_LABELS[t] for t in (
    EmailType.TRACING, EmailType.DRAFT, EmailType.WAYBILL, EmailType.ATB, EmailType.DSK))

# 正文分隔线（设计稿用的是 20 个全角粗横线）
_SEP = "━" * 20

_FILE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 小工具

def _now_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _day_str(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _weekday_cn(day: str) -> str:
    try:
        return "周一周二周三周四周五周六周日"[
            datetime.strptime(day, "%Y-%m-%d").weekday() * 2:][:2]
    except Exception:
        return ""


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in str(text))


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _FILE_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def _to_list_text(raw: str) -> str:
    """to_list 归一化：新表是 JSON 数组、旧表是逗号串；空数组归一成空串。

    新表 `to_list` 是 `json.dumps(list)`（`["a@x.com", "b@y.com"]`），旧表是逗号串
    （`a@x.com,b@y.com`）。这层不归一，`"[]"` 是非空字符串 ⇒ collect() 里
    `failed`（to_list 为空的行数）永远算 0。
    """
    text = (raw or "").strip()
    if not text.startswith("["):
        return text                      # 旧表逗号串 / 已损坏的串：原样返回，不抛
    try:
        arr = json.loads(text)
    except Exception:
        return text
    if not isinstance(arr, list):
        return text
    # x 为 None（JSON null）时直接跳过：正文里出现 "None" 是验收判据 6 明确禁止的。
    return ", ".join(str(x).strip() for x in arr if x is not None and str(x).strip())


def _ro_connect(path: str):
    """只读打开（绝不在缺文件时新建空库，也不改 WAL）。

    🔴 网络盘不可达时 `Path.exists()` 会抛 `OSError`（WinError 64 网络名不再可用），
    不能让它冒出去把整封邮件打掉 —— 退化成「这一类来源取不到数」，其余照发。
    """
    try:
        p = Path(str(path))
        if not p.exists():
            return None
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    except (OSError, sqlite3.Error) as e:
        _log.warning(f"daily report: cannot open {path}: {type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- 归档 stats（当天）

def write_archive_stats(stats: Dict[str, int], now: Optional[datetime] = None) -> None:
    """12:00 归档核对跑完后把 stats 落地（替代原来的企微 archive_summary）。"""
    today = _day_str(now)
    _write_json(Path(ARCHIVE_STATS_PATH), {
        "date": today,
        "at": _now_str(now),
        "stats": dict(stats or {}),
    })


def get_archive_stats(day: Optional[str] = None) -> Dict[str, int]:
    """读**当天**的归档 stats。日期不是当天 ⇒ 返回空（绝不拿昨天的充今天）。"""
    day = day or _day_str()
    data = _read_json(Path(ARCHIVE_STATS_PATH))
    if data.get("date") != day:
        return {}
    stats = data.get("stats") or {}
    return {k: int(v or 0) for k, v in stats.items()}


# ---------------------------------------------------------------- 转发明细（昨天）

def _owner_display(company: str) -> str:
    """公司 → 负责人中文姓名（bot_config scope='owner' + 邮箱→姓名映射）。查不到回退公司名。

    任务 18 抽出来的唯一实现：`_tracing_rows` 与 `_next_forward_rows` **必须**共用，
    各写一份必然漂移。惰性导入保持原样（避免 import 期写库），异常一律回退公司名。
    """
    company = (company or "").strip()
    if not company:
        return ""
    try:
        from .notify import WeComNotifier        # 邮箱 → 企微姓名：全仓唯一映射表
        from .store import get_responsible_person  # 惰性导入：避免 import 期写库

        email = get_responsible_person(company) or ""
        return WeComNotifier._WECOM_NAME_BY_EMAIL.get(email, "") or company
    except Exception:
        return company


def _legacy_forward_rows(day: str) -> List[Dict[str, Any]]:
    """旧系统 `mailbots/data/forward_log.db`（草单/运单号/ATB/DSK）。

    旧系统运踪**不写这张表**（它写 `yxo.db:tracing_log`，见下）。
    只读打开，文件不存在/表不存在一律返回空 —— 邮件本身绝不能因此发不出去。
    """
    rows: List[Dict[str, Any]] = []
    conn = _ro_connect(LEGACY_FORWARD_LOG_PATH)
    if conn is None:
        return rows
    try:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT robot, ts, owner, company, code, box, subject, to_list, note"
            " FROM forward_log WHERE day=? AND test=0 ORDER BY ts",
            (day,),
        ):
            d = dict(r)
            rows.append({
                "robot": (d.get("robot") or "?").strip(),
                "time": (d.get("ts") or "")[11:16],
                "owner": d.get("owner") or "",
                "company": d.get("company") or "",
                "code": d.get("code") or "",
                "box": d.get("box") or "",
                "subject": d.get("subject") or "",
                "to_list": _to_list_text(d.get("to_list") or ""),
                "note": d.get("note") or "",
            })
    except Exception as e:
        _log.warning(f"daily report: legacy forward_log read failed: {type(e).__name__}: {e}")
    finally:
        conn.close()
    return rows


def _next_forward_rows(day: str) -> List[Dict[str, Any]]:
    """新系统 `mailbots_next/data/bot_config.db` → forward_log（五类机器人）。

    任务 18：旧系统一停，`forward_log.db` 不再增长、`tracing_log` 也没人写
    （新系统运踪走单发路径落**新表**）⇒ 日报必须能读出这张表，否则切换后
    每天 12:30 会发一封「转发 0 票」的假报。

    🔴 按 `sent_local`（本地时间，写入侧新加）分天，**不许用 `sent_at`**：
    `sent_at` 是 UTC（`datetime('now')`），本地 00:00–08:00 的转发会被算进前一天。
    容错与旧源完全对齐：文件不存在 / 打不开 / 表不存在 / 没有 sent_local 列
    （未迁移的老库）⇒ WARNING + 返回 `[]`，绝不抛异常。
    """
    rows: List[Dict[str, Any]] = []
    conn = _ro_connect(BOT_CONFIG_DB_PATH)
    if conn is None:
        return rows
    try:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT robot, sent_local, company, code, box, train_no, subject, to_list"
            " FROM forward_log WHERE sent_local LIKE ? ORDER BY sent_local",
            (f"{day}%",),
        ):
            d = dict(r)
            company = d.get("company") or ""
            robot = (d.get("robot") or "").strip() or "?"
            subject = d.get("subject") or ""
            train_no = d.get("train_no") or ""
            if robot == ROBOT_LABELS[EmailType.TRACING] and train_no:
                subject = f"班列{train_no}"     # 与 _tracing_rows 的展示一致
            rows.append({
                "robot": robot,
                "time": (d.get("sent_local") or "")[11:16],
                "owner": _owner_display(company),
                "company": company,
                "code": d.get("code") or "",
                "box": d.get("box") or "",
                "subject": subject,
                "to_list": _to_list_text(d.get("to_list") or ""),
                "note": "",
            })
    except Exception as e:
        _log.warning(f"daily report: next forward_log read failed: {type(e).__name__}: {e}")
    finally:
        conn.close()
    return rows


def _tracing_rows(day: str) -> List[Dict[str, Any]]:
    """运踪转发（`yxo.db:tracing_log`）—— 旧系统与新系统都写这张表。

    一行 = 一封发给某公司的运踪副本（`log_id` = 班列_公司_时分秒）。
    `forward_detail` 形如 `发至:a@x.com,b@x.com | 由:sender@x.com`。

    🔴 为什么运踪要单独取：旧系统的运踪机器人**不写** `forward_log`（它写
    `tracing_log`），而任务 16 同时把运踪成功通知从企微撤掉了 —— 不在这里补上，
    「转发概览/明细」里运踪会永远是 0，等于把当天几十票运踪从所有渠道弄丢。
    """
    rows: List[Dict[str, Any]] = []
    conn = _ro_connect(YXO_DB_PATH)
    if conn is None:
        return rows
    try:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT train_no, company, mail_msg_id, forward_detail, log_date"
            " FROM tracing_log WHERE log_date LIKE ? ORDER BY log_date",
            (f"{day}%",),
        ):
            d = dict(r)
            detail = d.get("forward_detail") or ""
            to_list = ""
            if "发至:" in detail:
                to_list = detail.split("发至:", 1)[1].split("|", 1)[0].strip()
            company = d.get("company") or ""
            owner = _owner_display(company)   # 与 _next_forward_rows 共用（任务 18）
            rows.append({
                "robot": "运踪",
                "time": (d.get("log_date") or "")[11:16],
                "owner": owner or company or "(未匹配)",
                "company": company,
                "code": "",
                "box": "",
                "subject": f"班列{d.get('train_no') or ''}".strip(),
                "to_list": to_list,
                "note": "",
            })
    except Exception as e:
        _log.warning(f"daily report: tracing_log read failed: {type(e).__name__}: {e}")
    finally:
        conn.close()
    return rows


def collect_forward_rows(day: str) -> List[Dict[str, Any]]:
    """全部转发行：旧库四类 + 新库五类 + 运踪历史（tracing_log），按时间升序。

    🔴 **不做任何去重**（任务书 1.4）：旧表没有 message_id，跨表去重本就无从下手；
    新表一封邮件拆 N 家公司 = N 行同一个 message_id ⇒ 按 message_id 去重会把 N 票压成 1 票。
    票数口径就是「实际发出的一封转发邮件算 1 票」。
    """
    rows = _legacy_forward_rows(day) + _next_forward_rows(day) + _tracing_rows(day)
    rows.sort(key=lambda r: (r.get("time") or "", r.get("robot") or ""))
    return rows


# ---------------------------------------------------------------- 计数与汇总项

# 第四节固定列的计数键（其余非零计数另起一行「其它计数」，保证一条不丢）
_EXTRA_SKIP = {
    "post_send_bookkeeping_failed",
    "send_check_skip_no_id",
    "send_check_skip_no_sender",
    "send_check_failed",
    "send_check_alerted",
    "archive_copy_failed",
    "archive_store_deleted_failed",
    "archive_expunge_failed",
}


def _merge_counts(a: Dict[str, int], b: Dict[str, int]) -> Dict[str, int]:
    out = {k: int(v or 0) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + int(v or 0)
    return out


def collect(now: Optional[datetime] = None,
            forward_day: Optional[str] = None) -> Dict[str, Any]:
    """把 6 类来源全部取出来，交给 build_body（纯取数，不做任何发送）。

    日期口径写死：转发明细取 `forward_day`（默认**昨天**）；digest 计数 / 归档
    stats / 未分类 / 配置缺失 / 待办 / 程序错误取**当天**。
    """
    from datetime import timedelta

    now = now or datetime.now()
    today = _day_str(now)
    if not forward_day:
        forward_day = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    rows = collect_forward_rows(forward_day)
    by_robot: Dict[str, int] = {}
    by_owner: Dict[str, int] = {}
    for r in rows:
        by_robot[r["robot"]] = by_robot.get(r["robot"], 0) + 1
        by_owner[r["owner"]] = by_owner.get(r["owner"], 0) + 1
    failed = sum(1 for r in rows if not (r.get("to_list") or "").strip())

    # ② 09:00 digest 快照 + 09:00~12:30 的增量（flush 之后计数器从零开始，
    #    两段相加不重不漏）
    counts = _merge_counts(get_digest_snapshot(today), get_counters_snapshot(today))

    issues = get_issues(today)
    pending = [i for i in issues if i.get("kind") == "pending"]
    unclassified = [i for i in issues if i.get("kind") == "unclassified"]
    config_missing = [i for i in issues if i.get("kind") == "config_missing"]
    program_errors = [i for i in issues if i.get("kind") == "program_error"]

    return {
        "now": now,
        "today": today,
        "forward_day": forward_day,
        "rows": rows,
        "by_robot": by_robot,
        "by_owner": by_owner,
        "total": len(rows),
        "failed": failed,
        "success": len(rows) - failed,
        "counts": counts,
        "archive": get_archive_stats(today),
        "pending": pending,
        "unclassified": unclassified,
        "config_missing": config_missing,
        "program_errors": program_errors,
    }


def _abnormal_count(data: Dict[str, Any]) -> int:
    return (len(data["pending"]) + len(data["unclassified"])
            + len(data["config_missing"]) + len(data["program_errors"]))


# ---------------------------------------------------------------- 正文

def _section_one(data: Dict[str, Any]) -> List[str]:
    total = data["total"]
    failed = data["failed"]
    ar = data["archive"]
    n_pending = len(data["pending"])
    n_cm = len(data["config_missing"]) + len(data["unclassified"])
    n_pe = len(data["program_errors"])

    if total:
        fwd = f"转发 {total} 票" + ("（全部成功）" if not failed else f"（成功 {data['success']} / 失败 {failed}）")
    else:
        fwd = "转发 0 票"

    abn = f"异常 {n_pending + n_cm + n_pe} 条"
    parts = []
    if n_pending:
        parts.append(f"待办{n_pending}")
    if n_cm:
        parts.append(f"配置缺失{n_cm}")
    if n_pe:
        parts.append(f"程序错误{n_pe}")
    if parts:
        abn += "（" + " · ".join(parts) + "）"

    arc = (f"归档 核过{ar.get('verified', 0)} 已删{ar.get('deleted', 0)} "
           f"失败{ar.get('failed', 0)}")

    lines = [f"{fwd}｜{abn}｜{arc}"]

    bk = int(data["counts"].get("post_send_bookkeeping_failed", 0) or 0)
    if bk:
        lines.append(
            f"昨天有 {bk} 条「发信后记账失败」，客户邮件已正常发出、无需重发，"
            f"但对应的 DSK/ATB 时间戳或运踪记录可能没写回，见第四节。"
        )
    elif n_pending + n_cm + n_pe:
        lines.append("有需要你处理的事项，逐条见第三节。")
    else:
        lines.append("今天全部正常，无需处理。")
    return lines


def _section_two(data: Dict[str, Any]) -> List[str]:
    by_robot = data["by_robot"]
    lines = []
    for robot in ROBOT_ORDER:
        lines.append(f"  {_pad(robot, 10)}{by_robot.get(robot, 0)} 票")
    # 未知来源（旧库里 robot 为空/异常）也要显示，不能悄悄吞掉
    extra = {k: v for k, v in by_robot.items() if k not in ROBOT_ORDER}
    for k, v in sorted(extra.items()):
        lines.append(f"  {_pad(k, 10)}{v} 票")
    lines.append("  " + "─" * 16)
    lines.append(f"  {_pad('合计', 10)}{data['total']} 票"
                 f"（成功 {data['success']} / 失败 {data['failed']}）")
    lines.append("")
    owners = sorted(data["by_owner"].items(), key=lambda kv: (-kv[1], kv[0]))
    if owners:
        lines.append("  按负责人：" + " · ".join(f"{o} {n}" for o, n in owners))
    else:
        lines.append("  按负责人：无")
    return lines


def _section_three(data: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    pend = data["pending"]
    lines.append(f"【待办 {len(pend)} 条】"
                 + ("（同箱不同到站 / 箱不符，需人工确认；这些已实时发企微给你）" if pend else ""))
    for it in pend:
        lines.append(f"  · {it.get('at', '')}  {it.get('detail', '')}")
    lines.append("")

    unc = data["unclassified"]
    cm = data["config_missing"]
    lines.append(f"【配置缺失 / 未分类 {len(unc) + len(cm)} 条】"
                 + ("（原逐封弹企微的，今天汇总到这里）" if (unc or cm) else ""))
    for it in unc:
        lines.append(f"  · 未分类邮件 {it.get('detail', '')}")
    for it in cm:
        lines.append(f"  · 配置缺失 {it.get('detail', '')}")
    codes = {}
    for it in cm:
        codes[it.get("code") or ""] = codes.get(it.get("code") or "", 0) + 1
    lines.append("  · 路由失败 {r} · 无收件人 {n} · 无 SMTP 密码 {p}".format(
        r=int(data["counts"].get("routing_failed", 0) or 0),
        n=codes.get("no_recipients", 0),
        p=codes.get("no_smtp_credentials", 0),
    ))
    lines.append("")

    pe = data["program_errors"]
    lines.append(f"【程序错误 {len(pe)} 条】"
                 + ("（详见日志 error_id）" if pe else ""))
    for it in pe:
        lines.append(f"  · {it.get('at', '')}  {it.get('detail', '')}")
    return lines


def _section_four(data: Dict[str, Any]) -> List[str]:
    c = data["counts"]
    ar = data["archive"]
    lines = ["发信后自检：跳过(无匹配键) {a} · 跳过(缺发信账号) {b} · 抓取失败 {c2} · 告警 {d}".format(
        a=int(c.get("send_check_skip_no_id", 0) or 0),
        b=int(c.get("send_check_skip_no_sender", 0) or 0),
        c2=int(c.get("send_check_failed", 0) or 0),
        d=int(c.get("send_check_alerted", 0) or 0),
    )]
    bk = int(c.get("post_send_bookkeeping_failed", 0) or 0)
    if bk:
        lines.append(f"  ⚠ 发信后记账失败 {bk} 次：邮件已正常发给客户，不会重发；")
        lines.append("    但对应的 DSK/ATB 时间戳或运踪记录可能没写回，请按需人工核对。")
    lines.append("")
    lines.append("归档核对（{d} 12:00）：核过 {v} / 已删 {x} / 核对失败 {f} / 补记已删 {r} / 终止 {t}".format(
        d=data["today"],
        v=ar.get("verified", 0), x=ar.get("deleted", 0), f=ar.get("failed", 0),
        r=ar.get("recovered_deleted", 0), t=ar.get("terminated", 0),
    ))
    del_fail = (int(c.get("archive_store_deleted_failed", 0) or 0)
                + int(c.get("archive_expunge_failed", 0) or 0))
    lines.append(f"  归档复制失败 {int(c.get('archive_copy_failed', 0) or 0)}"
                 f" · 归档删除失败 {del_fail}")
    others = {k: v for k, v in sorted(c.items()) if v and k not in _EXTRA_SKIP}
    if others:
        lines.append("  其它计数：" + " · ".join(f"{k}={v}" for k, v in others.items()))
    return lines


def _one_forward_line(r: Dict[str, Any]) -> str:
    seg = [f"  {r.get('time') or '--:--'} [{r.get('robot') or '?'}]"]
    label_bits = []
    if r.get("subject") and r.get("robot") == "运踪":
        label_bits.append(r["subject"])
    if r.get("box"):
        label_bits.append(f"箱{r['box']}")
    if r.get("code"):
        label_bits.append(r["code"])
    if r.get("company"):
        label_bits.append(r["company"])
    if label_bits:
        seg.append(" ".join(label_bits))
    if r.get("to_list"):
        seg.append("发至 " + r["to_list"][:80])
    if r.get("note"):
        seg.append(f"（{r['note'][:40]}）")
    return " ".join(seg)


def _section_five(data: Dict[str, Any]) -> List[str]:
    rows = data["rows"]
    if not rows:
        return ["昨日无转发。"]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r.get("owner") or "(未匹配)", []).append(r)
    lines: List[str] = []
    for owner in sorted(grouped, key=lambda o: (-len(grouped[o]), o)):
        lines.append(f"【{owner}】{len(grouped[owner])} 票")
        for r in sorted(grouped[owner], key=lambda x: x.get("time") or ""):
            lines.append(_one_forward_line(r))
        lines.append("")
    if lines and not lines[-1]:
        lines.pop()
    return lines


def build_body(data: Optional[Dict[str, Any]] = None) -> str:
    """整封正文（五节，从头生成，不拼接旧日报文本）。"""
    data = data if data is not None else collect()
    day = data["forward_day"]
    lines = [f"【每日汇总】{day}（{_weekday_cn(day)}）".replace("（）", ""), ""]

    def _block(title: str, body: List[str]) -> None:
        lines.append(_SEP)
        lines.append(f"■ {title}")
        lines.append(_SEP)
        lines.extend(body)
        lines.append("")

    _block("一、今天一句话（先看这里）", _section_one(data))
    _block("二、转发概览（粗）", _section_two(data))
    _block("三、待办与异常（细 · 需你处理的都在这）", _section_three(data))
    _block("四、发送与归档自检（细 · 只报异常数，正常为 0）", _section_four(data))
    _block("五、转发明细（细 · 全量，按需看）", _section_five(data))

    lines.append(_SEP)
    lines.append("本邮件每天 12:30 自动发送，无需回复。")
    lines.append("发现漏转/错转，在企微或微信里告诉订舱助手。")
    return "\n".join(lines)


def build_subject(data: Optional[Dict[str, Any]] = None) -> str:
    data = data if data is not None else collect()
    return (f"[每日汇总] {data['forward_day']} 转发{data['total']} "
            f"异常{_abnormal_count(data)}")


# ---------------------------------------------------------------- 发送

def _smtp_send(subject: str, body: str, to_list: List[str]) -> None:
    from ..config.secrets import get_accounts

    password = (get_accounts() or {}).get(OPS_OWNER_EMAIL, "")
    if not password:
        raise RuntimeError(f"No SMTP password for {OPS_OWNER_EMAIL}")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = OPS_OWNER_EMAIL
    msg["To"] = ", ".join(to_list)
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
        s.login(OPS_OWNER_EMAIL, password)
        refused = s.sendmail(OPS_OWNER_EMAIL, to_list, msg.as_string())
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def _sent_state() -> Dict[str, Any]:
    return _read_json(Path(DAILY_REPORT_SENT_PATH))


def is_due(now: Optional[datetime] = None) -> bool:
    """是否已到 12:30（固定发送时点）。"""
    now = now or datetime.now()
    return (now.hour, now.minute) >= (DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE)


def send_daily_report(now: Optional[datetime] = None, day: Optional[str] = None,
                      force: bool = False, dry: bool = False) -> Dict[str, Any]:
    """发当天唯一那封邮件。

    返回 `{"sent": bool, "reason": str, "subject": str, "body": str}`：
      reason = not_due / already_sent / dry / sent / send_failed
    `day` 只用于人工补发（默认按 now 前推一天取转发行）。
    """
    now = now or datetime.now()
    data = collect(now, forward_day=day)
    subject = build_subject(data)
    body = build_body(data)
    today = _day_str(now)

    if dry:
        return {"sent": False, "reason": "dry", "subject": subject, "body": body}
    if not force and not is_due(now):
        return {"sent": False, "reason": "not_due", "subject": subject, "body": body}

    state = _sent_state()
    if state.get("date") == today and not force:
        return {"sent": False, "reason": "already_sent", "subject": subject, "body": body}

    try:
        _smtp_send(subject, body, [OPS_OWNER_EMAIL])
    except Exception as e:
        _log.error(f"Daily report send failed: {type(e).__name__}: {e}")
        return {"sent": False, "reason": "send_failed", "subject": subject, "body": body}

    _write_json(Path(DAILY_REPORT_SENT_PATH),
                {"date": today, "at": _now_str(now), "subject": subject})
    _log.info(f"Daily report sent | date={today} | {subject}")
    return {"sent": True, "reason": "sent", "subject": subject, "body": body}
