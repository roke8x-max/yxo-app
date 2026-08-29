# -*- coding: utf-8 -*-
"""
cs_bot 会话引擎：查询 / 更新（本地+飞书双写）/ 文件归档 / 邮件草稿 / 撤销。
入口：
    handle_text(user, text)  -> reply str
    handle_file(user, path)  -> reply str
user 为真名（毛骁洋/冯茜/杨雅雯/韩文豪）；未绑定用户传入原始 ID，只能用「绑定 姓名」。
"""
import os
import re
import sys
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, r"D:\YXO_DATA\MailBots")
from common_io import atomic_write_json
from config import (USER_COMPANIES, ADMIN_USERS, FIELD_OPTIONS, CS_STAGING_DIR,
                    CS_BOT_DB)

from cs_bot import parser, store, files, mailer

# 每用户会话: {draft, pending, staged, batch, replace_idx}
_sessions = {}

# ---- 会话落盘（2026-07-29 芙蕾雅改造）----
# 进行中的草稿/暂存/待确认写入 data/sessions.json，服务重启后自动恢复，
# 实现设计文档要求的「重启不丢会话」。读写失败均不影响正常回复。
_SESS_FILE = os.path.join(os.path.dirname(CS_BOT_DB), "sessions.json")
_sess_lock = threading.Lock()


def _load_sessions():
    try:
        with open(_SESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _sessions.update(data)
    except Exception:
        pass  # 文件不存在/损坏 → 从空会话开始


def _save_sessions():
    try:
        with _sess_lock:
            os.makedirs(os.path.dirname(_SESS_FILE), exist_ok=True)
            atomic_write_json(_SESS_FILE, _sessions)
    except Exception:
        pass  # 落盘失败不影响回复


_load_sessions()

HELP_TEXT = """📖 指令说明
① 查询：直接发 客编 / 箱号 / 班列号（如 WB759）
② 更新箱号：客编 更新箱号 XXXX1234567（格式不限）
③ 更新状态：客编 草单 已确认 / 报放单 已上传 / 随车 未收
   （草单：未出/已确认/欧线；报放单：未出/已上传；随车：未收/已发邮件）
④ 存文件：直接发文件，按提示回复用途；或先发「存档 客编」再连续发文件，最后回「完毕」
⑤ 发邮件：发邮件 客编 → 预览草稿 → 编辑 → 确认发送
⑥ 撤销：回复「撤销」回退上一步写操作
⑦ 迷路了：回复「？！」查看当前环节和可用指令
⑧ 不想做了：回复「主页」立即放弃当前操作
⑨ 统计：统计 8月 太平洋 货源（月份可写 7-8月，公司可多个，不写=全部）
⑩ 改客编：改客编 <旧客编> <新客编>（随到站变化改客编）
⑪ 交换客编：交换客编 <客编A> <客编B>（两条客编信息对调）
⑫ 草单待确认：待办（看挂起的草单）/ 确认 编号 / 跳过 编号
   批量：确认全部（二次确认「确认全部 确定」）/ 全部跳过 / 跳过我的 / 跳过别人的 / 跳过 姓名
"""


# ==================== 草单待确认（v3.3 芙蕾雅） ====================
# 队列与操作逻辑在 MailBots/draft_pending.py（与草单转发机器人共用）。
# 懒加载：MailBots 不可用时不影响其他功能。

def _get_draft_pending():
    try:
        import draft_pending
        return draft_pending
    except ImportError:
        _base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _mb = os.path.join(_base, "MailBots")
        if os.path.isdir(_mb) and _mb not in sys.path:
            sys.path.insert(0, _mb)
        try:
            import draft_pending
            return draft_pending
        except Exception:
            return None
    except Exception:
        return None


def _draft_todo(user):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.format_todo(user)
    except Exception as e:
        return f"⚠ 查询待办失败：{e}"


def _draft_confirm(user, n):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.confirm(n, user)
    except Exception as e:
        return f"⚠ 确认 #{n} 处理异常：{e}\n单子未丢，可稍后重试。"


def _draft_skip(user, n):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.skip(n, user)
    except Exception as e:
        return f"⚠ 跳过 #{n} 处理异常：{e}"


def _draft_confirm_all(user, force=False):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.confirm_all(user, force=force)
    except Exception as e:
        return f"⚠ 确认全部处理异常：{e}\n单子未丢，可稍后重试。"


def _draft_skip_all(user):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.skip_all(user)
    except Exception as e:
        return f"⚠ 全部跳过处理异常：{e}"


def _draft_skip_mine(user):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.skip_mine(user)
    except Exception as e:
        return f"⚠ 跳过自己名下处理异常：{e}"


def _draft_skip_others(user):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.skip_others(user)
    except Exception as e:
        return f"⚠ 跳过他人名下处理异常：{e}"


def _draft_skip_owner(user, name):
    dp = _get_draft_pending()
    if not dp:
        return "⚠ 草单待确认模块暂不可用，请联系毛骁洋检查 MailBots 目录。"
    try:
        return dp.skip_owner(user, name)
    except Exception as e:
        return f"⚠ 跳过指定同事处理异常：{e}"


def _sess(user):
    if user not in _sessions:
        _sessions[user] = {"draft": None, "pending": None, "staged": [],
                           "batch": None, "replace_idx": None}
    return _sessions[user]


def _known(user):
    return user in USER_COMPANIES


def _has_perm(user, rec):
    if user in ADMIN_USERS:
        return True
    comp = str(rec.get("开票子公司名称") or "")
    for kw in USER_COMPANIES.get(user, []):
        if kw in comp:
            return True
    return False


def _perm_denied(user, rec):
    comp = rec.get("开票子公司名称") or "（空）"
    mine = "、".join(USER_COMPANIES.get(user, [])) or "无"
    return (f"⛔ 无权限：{rec.get('客户编码')} 属于「{comp}」，"
            f"你管理的公司是：{mine}。如需操作请联系毛骁洋。")


def _find_one(code):
    """按客编查唯一记录。返回 (rec, err_text)。"""
    recs = store.find_by_code(code)
    if not recs:
        return None, f"❌ 未找到客编 {code} 的记录，请检查编号。"
    if len(recs) > 1:
        lines = [f"⚠ 客编 {code} 匹配到 {len(recs)} 条，请发完整客编："]
        lines += ["· " + store.format_record(r, brief=True) for r in recs[:8]]
        return None, "\n".join(lines)
    return recs[0], None


# ==================== 文本入口 ====================

def handle_text(user, text):
    """公开入口：处理完落盘会话（重启不丢）。"""
    try:
        return _handle_text_impl(user, text)
    finally:
        _save_sessions()


def _handle_text_impl(user, text):
    s = _sess(user)
    text = (text or "").strip()
    if not text:
        return "❓ 空消息。回复「帮助」查看指令。"

    p = parser.parse(text)

    # ---- 未绑定用户 ----
    if not _known(user):
        if p["intent"] == "bind":
            if p["name"] in USER_COMPANIES:
                return "__BIND__" + p["name"]     # server 层完成绑定
            return f"❌ 「{p['name']}」不在使用名单中（毛骁洋/冯茜/杨雅雯/韩文豪）。"
        return "👋 你还未绑定身份，请回复：绑定 你的姓名（如：绑定 冯茜）"

    # ---- 草单待确认命令（2026-07-29 芙蕾雅 v3.3 / 2026-07-31 批量增强）----
    # 严格正则拦截, 不经过 parser, 不影响既有意图
    plain0 = text.replace(" ", "")
    if plain0 in ("待办", "待确认", "草单待办"):
        return _draft_todo(user)
    # 确认 / 跳过 单条（按编号）
    m0 = re.match(r"^确认\s*(\d+)$", text.strip())
    if m0:
        return _draft_confirm(user, int(m0.group(1)))
    m0 = re.match(r"^跳过\s*(\d+)$", text.strip())
    if m0:
        return _draft_skip(user, int(m0.group(1)))
    # 确认全部（带二次确认护栏）
    if plain0 in ("确认全部确定", "全部确认确定"):
        return _draft_confirm_all(user, force=True)
    if plain0 in ("确认全部", "全部确认"):
        return _draft_confirm_all(user, force=False)
    # 全部跳过
    if plain0 in ("全部跳过", "跳过全部"):
        return _draft_skip_all(user)
    # 只跳过自己 / 别人
    if plain0 in ("跳过我的", "跳过自己", "跳过本人"):
        return _draft_skip_mine(user)
    if plain0 in ("跳过别人的", "跳过他人", "跳过其他", "跳过别人"):
        return _draft_skip_others(user)
    # 跳过指定同事：跳过 杨雅雯 / 跳过@杨雅雯
    m1 = re.match(r"^跳过\s*[@:]?\s*([^\d].*)$", text.strip())
    if m1:
        return _draft_skip_owner(user, m1.group(1).strip())

    # ---- 撤销 / 帮助 / 状态 / 主页（任何状态可用）----
    if p["intent"] == "undo":
        return _do_undo(user)
    if p["intent"] == "help":
        return HELP_TEXT
    if p["intent"] == "status":
        return _do_status(user, s)
    if p["intent"] == "home":
        return _go_home(user, s)
    if p["intent"] == "bind":
        return f"✅ 你已经是 {user}，无需重复绑定。"

    # ---- 草稿模式优先 ----
    if s["draft"]:
        reply = _draft_command(user, s, text, p)
        if reply is not None:
            return reply

    # ---- 待回答问题 ----
    if s["pending"]:
        reply = _answer_pending(user, s, text, p)
        if reply is not None:
            return reply

    # ---- 批量存档模式 ----
    if s["batch"]:
        plain = text.replace(" ", "")
        if plain in ("完毕", "完成", "结束", "好了"):
            return _batch_finish(user, s)
        if plain in ("取消",):
            return _batch_cancel(s)

    # ---- 常规意图 ----
    if p["intent"] == "mail":
        return _start_mail(user, s, p)
    if p["intent"] == "archive":
        if not p["code"]:
            return "❓ 存档请带上客编，例如：存档 CQWLJT260819001-D 随车"
        return _start_archive(user, s, p)
    if p["intent"] == "list_folder":
        return _list_folder_cmd(p)
    if p["intent"] == "update":
        return _do_update(user, p)
    if p["intent"] == "change_code":
        return _do_change_code(user, s, p)
    if p["intent"] == "swap_code":
        return _do_swap_code(user, s, p)
    if p["intent"] == "ambiguous":
        return _resolve_ambiguous(user, s, p)
    if p["intent"] == "train_query":
        return _do_train_query(p)
    if p["intent"] == "stats":
        return _do_stats(p, text)
    if p["intent"] == "query":
        return _do_query(user, s, p)

    return "🤔 没看懂这条指令。回复「帮助」查看用法，或换个说法试试。"


# ==================== 状态查询「？！」 ====================

def _do_status(user, s):
    lines = []
    if s["draft"]:
        d = s["draft"]
        lines.append(f"📍 当前环节：邮件草稿编辑中（{d['code']}）")
        lines.append(f"主题：{d['subject']}")
        lines.append(f"附件：{len(d['attachments'])} 个")
        lines.append("")
        lines.append("现在可以：")
        lines.append("· 更新正文 xxx ｜ 更新收件 xxx ｜ 更新抄送 xxx")
        lines.append("· 直接发文件 = 新增附件")
        lines.append("· 删除附件 N ｜ 替换附件 N")
        lines.append("· 查看文件夹 → 回复「加 N」添加为附件")
        lines.append("· 预览 ｜ 确认发送 ｜ 取消")
    elif s["pending"] and s["pending"]["type"] == "box_conflict":
        pd = s["pending"]
        lines.append(f"📍 当前环节：箱号冲突待确认（{pd['code']}）")
        lines.append(f"库里箱号：{pd['old_box']}，你发的：{pd['new_box']}")
        lines.append("")
        lines.append("现在可以：")
        lines.append("· 1 = 更新为新箱号")
        lines.append("· 2 = 重新查询")
        lines.append("· 3 = 展示该客编信息")
        lines.append("· 取消")
    elif s["pending"] and s["pending"]["type"] == "code_letter_diff":
        pd = s["pending"]
        lines.append(f"📍 当前环节：客编字母不符待确认（输入 {pd['input']}）")
        lines.append("库内数字段相同、字母不同的记录：")
        for c in pd["candidates"]:
            lines.append(f"· {c}")
        lines.append("")
        lines.append("现在可以：")
        lines.append("· 1 = 将库内客编更新为 " + pd["input"])
        lines.append("· 2 = 仅查看库内记录")
        lines.append("· 取消")
    elif s["pending"] and s["pending"]["type"] == "file_purpose":
        lines.append(f"📍 当前环节：等待说明文件用途（已暂存 {len(s['staged'])} 个文件）")
        lines.append("")
        lines.append("现在可以：")
        lines.append("· 存档 客编 [随车/报放]")
        lines.append("· 发邮件 客编")
        lines.append("· 取消（发错了，文件移回收区）")
    elif s["batch"]:
        b = s["batch"]
        lines.append(f"📍 当前环节：批量存档模式（{b['code']} / {b['sub']}）")
        lines.append(f"已暂存 {len(s['staged'])} 个文件")
        lines.append("")
        lines.append("现在可以：")
        lines.append("· 继续发文件")
        lines.append("· 完毕（全部入库）")
        lines.append("· 取消（文件移回收区）")
    else:
        lines.append("📍 当前在主页，没有进行中的操作。")
        lines.append("")
        lines.append("可以直接：")
        lines.append("· 发 客编 / 箱号 / 班列号（如 WB759）查询")
        lines.append("· 客编 + 字段 + 值 更新记录")
        lines.append("· 发文件存档 ｜ 发邮件 客编")
        lines.append("· 回复「帮助」看完整指令")
        if s["staged"]:
            lines.append(f"⚠ 暂存区还有 {len(s['staged'])} 个文件未处理")
    lines.append("")
    lines.append("🏠 回复「主页」可随时放弃当前操作。")
    return "\n".join(lines)


# ==================== 返回主页 ====================

def _go_home(user, s):
    notes = []
    if s["draft"]:
        s["draft"] = None
        s["replace_idx"] = None
        notes.append("· 邮件草稿已丢弃（文件未删除）")
    if s["pending"]:
        s["pending"] = None
        notes.append("· 待确认的问题已取消")
    if s["batch"]:
        s["batch"] = None
        notes.append("· 已退出批量存档模式")
    if s["staged"]:
        n = 0
        for path in s["staged"]:
            if os.path.isfile(path):
                files.trash_file(path)
                n += 1
        s["staged"] = []
        notes.append(f"· {n} 个暂存文件已移入回收区")
    if not notes:
        return "🏠 你已经在主页了，没有进行中的操作。直接发指令即可。"
    return "🏠 已返回主页：\n" + "\n".join(notes)


# ==================== 查询 ====================

def _do_query(user, s, p):
    if p["code"]:
        recs = store.find_by_code(p["code"])
        if not recs:
            # 数字段命中、但 '-' 后到站字母不同 → 询问是否更新客编
            cands = store.find_by_code_core(p["code"])
            if cands:
                codes = [c["客户编码"] for c in cands]
                s["pending"] = {"type": "code_letter_diff", "input": p["code"],
                                "candidates": codes}
                lines = [f"⚠ 你输入的客编 {p['code']} 与库内以下记录「数字段一致、但到站字母不同」："]
                for c in codes:
                    lines.append(f"· {c}")
                lines.append(f"是否将库内客编更新为 {p['code']}？")
                lines.append("回复：1=更新库内客编  2=仅查看库内记录  3=取消")
                return "\n".join(lines)
            return f"❌ 未找到客编 {p['code']} 的记录。"
        if len(recs) == 1:
            return "🔍 查询结果：\n" + store.format_record(recs[0])
        lines = [f"🔍 匹配到 {len(recs)} 条："]
        lines += ["· " + store.format_record(r, brief=True) for r in recs[:8]]
        return "\n".join(lines)
    if p["box"]:
        recs = store.find_by_box(p["box"])
        if not recs:
            return f"❌ 未找到箱号 {p['box']} 的记录。"
        if len(recs) == 1:
            return "🔍 查询结果：\n" + store.format_record(recs[0])
        lines = [f"🔍 箱号 {p['box']} 匹配到 {len(recs)} 条："]
        lines += ["· " + store.format_record(r, brief=True) for r in recs[:8]]
        return "\n".join(lines)
    return "❓ 请提供客编或箱号，例如直接发：CQWLJT260819001-D"


# ==================== 统计 ====================

# 货源类型展示顺序（未出现的不显示；库里新增类型自动跟进）
_SOURCE_ORDER = ["本地", "绕园", "外地"]


def _fmt_months_label(months):
    """['2026/07','2026/08'] -> '2026年7-8月'；跨年则完整列出。"""
    if not months:
        return ""
    ys = sorted({m[:4] for m in months})
    if len(ys) == 1:
        nums = sorted(int(m[5:7]) for m in months)
        if len(nums) == 1:
            return f"{ys[0]}年{nums[0]}月"
        # 连续则用范围，不连续则逗号列出
        if nums == list(range(nums[0], nums[-1] + 1)):
            return f"{ys[0]}年{nums[0]}-{nums[-1]}月"
        return f"{ys[0]}年" + "、".join(f"{n}月" for n in nums)
    return "、".join(m.replace("/", "年") + "月" for m in sorted(months))


def _do_stats(p, text):
    months = p.get("stat_months") or []
    if not months:
        return ("📊 统计需要带上月份，例如：\n"
                "· 统计 8月 太平洋 货源\n"
                "· 统计 7-8月 太平洋 保时达 货源\n"
                "（不写公司 = 统计全部公司）")

    # 从原文里识别公司（支持多个；不写 = 全部）
    all_comps = store.distinct_companies()
    comps = [c for c in all_comps if c in text]

    data = store.stats_source(months, comps or None)
    label = _fmt_months_label(months)
    scope = "、".join(comps) if comps else "全部公司"

    if not data:
        return f"📊 {label} ｜ {scope}\n没有找到符合条件的记录。"

    # 汇总
    total = {}
    for comp_stats in data.values():
        for src, n in comp_stats.items():
            total[src] = total.get(src, 0) + n
    grand = sum(total.values())

    def _fmt_block(title, st):
        cnt = sum(st.values())
        lines = [f"▪️ {title}（{cnt} 票）"]
        keys = [k for k in _SOURCE_ORDER if k in st] + \
               [k for k in st if k not in _SOURCE_ORDER and k != "未填"] + \
               (["未填"] if "未填" in st else [])
        for k in keys:
            lines.append(f"　· {k}：{st[k]} 票")
        local = st.get("本地", 0)
        if cnt:
            lines.append(f"　本地货源占比：{local * 100 / cnt:.1f}%")
        return "\n".join(lines)

    blocks = [f"📊 货源统计 ｜ {label}\n🏢 范围：{scope} ｜ 共 {grand} 票"]
    if len(data) == 1:
        comp, st = next(iter(data.items()))
        blocks.append(_fmt_block(comp, st))
    else:
        for comp in sorted(data.keys()):
            blocks.append(_fmt_block(comp, data[comp]))
        blocks.append(_fmt_block("合计", total))
    return "\n\n".join(blocks)


# ==================== 班列号查询 ====================

def _do_train_query(p):
    """班列查询：只展示发班时间最新的一班（手机端只关心最新业务）。
    历史同名班次只提示数量，需要细看历史时上电脑处理。"""
    train = p["train"]
    months = store.find_by_train(train)
    if not months:
        return f"❌ 未找到班列 {train} 的记录，请检查班列号。"

    # find_by_train 按年月倒序返回，第一个分组即最新一班
    latest_month = next(iter(months))
    recs = months[latest_month]
    older_total = sum(len(v) for k, v in months.items() if k != latest_month)

    dates = sorted({str(r.get("发班时间") or "").strip() for r in recs} - {""})
    ports = sorted({str(r.get("口岸") or "").strip() for r in recs} - {""})
    dests = sorted({str(r.get("目的站") or "").strip() for r in recs} - {""})
    head = f"🚂 班列 {train} ｜ {len(recs)} 票"
    if dates:
        head += f"\n📅 发班：{' / '.join(dates)}"
    port_uniform = len(ports) <= 1
    dest_uniform = len(dests) <= 1
    if port_uniform and ports:
        head += f"\n🛃 口岸：{ports[0]}"
    if dest_uniform and dests:
        head += f"\n🎯 目的站：{dests[0]}"

    # 按公司分组
    groups = {}
    for r in recs:
        comp = str(r.get("开票子公司名称") or "").strip() or "（未填公司）"
        groups.setdefault(comp, []).append(r)

    blocks = [head]
    for comp, items in groups.items():
        lines = [f"▪️ {comp}（{len(items)} 票）"]
        for i, r in enumerate(items, 1):
            box = str(r.get("箱号") or "").strip() or "—"
            lines.append(f"{i}. {r.get('客户编码', '')} ｜ 箱号：{box}")
            extra = []
            if not port_uniform:
                extra.append(f"口岸：{str(r.get('口岸') or '').strip() or '—'}")
            if not dest_uniform:
                extra.append(f"目的站：{str(r.get('目的站') or '').strip() or '—'}")
            if extra:
                lines.append("    " + " ｜ ".join(extra))
        blocks.append("\n".join(lines))

    if older_total:
        blocks.append(f"ℹ️ 另有 {older_total} 票更早的同名班次记录（含往年同名班列，请在电脑端查看）")

    return "\n\n".join(blocks)


# ==================== 歧义：客编+箱号 ====================

def _resolve_ambiguous(user, s, p):
    rec, err = _find_one(p["code"])
    if err:
        return err
    db_box = str(rec.get("箱号") or "").strip().upper()
    new_box = p["box"].upper()
    if db_box == new_box:
        return "🔍 查询结果：\n" + store.format_record(rec)
    if not db_box:
        # 库里箱号为空 → 更新
        return _write_fields(user, rec, {"箱号": p["box"]})
    # 不一致 → 反问
    s["pending"] = {"type": "box_conflict", "rec_id": rec["id"],
                    "code": rec["客户编码"], "new_box": p["box"], "old_box": db_box}
    return (f"⚠ 客编 {rec['客户编码']} 库里箱号是 {db_box}，你发的是 {new_box}。\n"
            f"回复：1=更新为新箱号  2=重新查询  3=展示该客编信息")


# ==================== 更新（双写） ====================

def _do_update(user, p):
    if not p["code"]:
        if p["box"]:
            recs = store.find_by_box(p["box"])
            if len(recs) == 1:
                rec = recs[0]
            else:
                return "❓ 更新指令请带上客编，例如：CQWLJT260819001-D 草单 已确认"
        else:
            return "❓ 更新指令请带上客编，例如：CQWLJT260819001-D 草单 已确认"
    else:
        rec, err = _find_one(p["code"])
        if err:
            return err

    # 字段值缺失（提到字段但没给合法选项）
    for f, v in p["fields"].items():
        if v is None:
            opts = " / ".join(FIELD_OPTIONS.get(f, []))
            return f"❓ {f} 只能写以下选项之一：{opts}\n例如：{rec['客户编码']} {f} {FIELD_OPTIONS[f][0]}"

    return _write_fields(user, rec, p["fields"])


def _write_fields(user, rec, fields):
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)

    code = rec["客户编码"]
    # 1) 本地库
    old, ok = store.update_record(rec["id"], fields, user)
    if not ok:
        return f"❌ 本地库更新失败（记录不存在）。"
    # 2) 飞书双写（已弃用，保留本地库为准）
    fs_ok, fs_record_id, fs_old, fs_err = True, None, {}, ""

    store.push_op(user, "db_update", {
        "rec_id": rec["id"], "code": code, "fields": fields, "old": old,
        "fs_record_id": fs_record_id, "fs_old": fs_old, "fs_ok": fs_ok,
    })

    changes = "，".join(f"{k}：{old.get(k) or '—'} → {v if v != '' else '（清空）'}"
                       for k, v in fields.items())
    lines = [f"✅ 已更新 {code}", changes,
             f"本地库 ✅ | 飞书 {'✅' if fs_ok else '❌ ' + fs_err}"]
    if not fs_ok:
        lines.append("⚠ 飞书未同步成功，请稍后在飞书里核对该条。")
    lines.append("（回复「撤销」可回退）")
    return "\n".join(lines)


# ==================== 改客编 / 交换客编 ====================

def _do_change_code(user, s, p):
    """改客编：把一条记录的客户编码改为新值。"""
    old = p["code"]
    new = p["code2"]
    if not old or not new:
        return ("❓ 改客编格式：改客编 <旧客编> <新客编>\n"
                "例如：改客编 CQWLJT260808001-VXN CQWLJT260808001-VXO")
    if old.upper() == new.upper():
        return "❓ 新旧客编相同，无需修改。"
    rec, err = _find_one(old)
    if err:
        return err
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)
    # 新客编是否已存在（避免覆盖别的记录）
    exist = [e for e in store.find_by_code(new)
             if str(e.get("客户编码", "")).upper() != old.upper()]
    if exist:
        return f"⛔ 新客编 {new} 已存在（{len(exist)} 条），不能覆盖，请先处理冲突。"
    s["pending"] = {"type": "change_code", "rec_id": rec["id"], "code": old, "new": new}
    return (f"⚠ 确认把客编\n  {old}\n改为\n  {new}\n？\n"
            f"（公司：{rec.get('开票子公司名称') or '—'}）\n"
            f"回复 1 确认，其他任意内容取消。")


def _commit_change_code(user, pd):
    rec, err = _find_one(pd["code"])
    if err:
        return err
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)
    old_vals, ok = store.update_record(rec["id"], {"客户编码": pd["new"]}, user)
    if not ok:
        return "❌ 本地库更新失败。"
    # 飞书双写（已弃用，本地库为准）
    fs_ok, fs_rid, fs_old, fs_err = True, None, {}, ""
    store.push_op(user, "db_update", {
        "rec_id": rec["id"], "code": pd["new"],
        "fields": {"客户编码": pd["new"]},
        "old": {"客户编码": pd["code"]},
        "fs_record_id": fs_rid, "fs_old": {"客户编码": pd["code"]},
        "fs_ok": fs_ok})
    lines = [f"✅ 客编已修改：{pd['code']} → {pd['new']}",
             f"本地库 ✅ | 飞书 {'✅' if fs_ok else '❌ ' + fs_err}",
             "（回复「撤销」可回退）"]
    return "\n".join(lines)


def _do_swap_code(user, s, p):
    """交换客编：把两条记录的客户编码对调。"""
    a, b = p["code"], p["code2"]
    if not a or not b:
        return "❓ 交换客编格式：交换客编 <客编A> <客编B>"
    if a.upper() == b.upper():
        return "❓ 两个客编相同，无需交换。"
    recA, errA = _find_one(a)
    if errA:
        return errA
    recB, errB = _find_one(b)
    if errB:
        return errB
    if not _has_perm(user, recA) or not _has_perm(user, recB):
        bad = recA if not _has_perm(user, recA) else recB
        return _perm_denied(user, bad)
    s["pending"] = {"type": "swap_code",
                    "a_id": recA["id"], "b_id": recB["id"],
                    "a_code": a, "b_code": b,
                    "a_box": recA.get("箱号"), "b_box": recB.get("箱号")}
    return (f"⚠ 确认交换两条客编：\n  {a}（{recA.get('开票子公司名称') or '—'}）\n"
            f"  {b}（{recB.get('开票子公司名称') or '—'}）\n"
            f"交换后：{a} ↔ {b}\n"
            f"回复 1 确认，其他任意内容取消。")


def _commit_swap_code(user, pd):
    recA, errA = _find_one(pd["a_code"])
    recB, errB = _find_one(pd["b_code"])
    if errA:
        return errA
    if errB:
        return errB
    if not _has_perm(user, recA) or not _has_perm(user, recB):
        return "⛔ 无权限执行交换。"
    store.update_record(recA["id"], {"客户编码": pd["b_code"]}, user)
    store.update_record(recB["id"], {"客户编码": pd["a_code"]}, user)
    # 飞书双写（已弃用）
    fs_ok_a, _, _, _ = True, None, None, None
    fs_ok_b, _, _, _ = True, None, None, None
    fs_ok = fs_ok_a and fs_ok_b
    store.push_op(user, "swap_code", {
        "a_id": recA["id"], "b_id": recB["id"],
        "a_code": pd["a_code"], "b_code": pd["b_code"],
        "a_box": pd["a_box"], "b_box": pd["b_box"], "fs_ok": fs_ok})
    return (f"✅ 客编已交换：{pd['a_code']} ↔ {pd['b_code']}\n"
            f"本地库 ✅ | 飞书 {'✅' if fs_ok else '❌ 请手动核对'}\n"
            f"（回复「撤销」可回退）")


# ==================== 撤销 ====================

def _do_undo(user):
    op = store.pop_op(user)
    if not op:
        return "ℹ 没有可撤销的操作。"
    kind, pl = op["kind"], op["payload"]

    if kind == "db_update":
        store.update_record(pl["rec_id"], pl["old"], user + "(撤销)")
        fs_note = ""
        # 飞书撤销（已弃用）
        ok = True
        if pl.get("fs_ok") and pl.get("fs_record_id"):
            fs_note = " | 飞书 " + ("✅" if ok else "❌ 请手动核对")
        store.mark_undone(op["id"])
        restored = "，".join(f"{k}={v or '—'}" for k, v in pl["old"].items())
        return f"↩ 已撤销对 {pl['code']} 的更新（恢复为 {restored}）本地 ✅{fs_note}"

    if kind == "file_save":
        moved = []
        for path in pl.get("paths", []):
            if os.path.isfile(path):
                moved.append(os.path.basename(files.trash_file(path)))
        store.mark_undone(op["id"])
        return (f"↩ 已撤销存档，{len(moved)} 个文件移入回收区（未物理删除）：\n"
                + "\n".join("· " + m for m in moved))

    if kind == "mail_sent":
        store.mark_undone(op["id"])
        return "⚠ 邮件已发出，无法撤回。如需补救请直接联系收件人。"

    if kind == "swap_code":
        pl2 = op["payload"]
        store.update_record(pl2["a_id"], {"客户编码": pl2["a_code"]}, user + "(撤销)")
        store.update_record(pl2["b_id"], {"客户编码": pl2["b_code"]}, user + "(撤销)")
        store.mark_undone(op["id"])
        return f"↩ 已撤销客编交换：{pl2['a_code']} ↔ {pl2['b_code']} 本地 ✅ | 飞书 ✅"

    store.mark_undone(op["id"])
    return "ℹ 该操作无需撤销。"


# ==================== 待回答问题 ====================

def _answer_pending(user, s, text, p):
    pd = s["pending"]
    plain = text.replace(" ", "")

    if plain in ("取消", "算了"):
        s["pending"] = None
        if pd["type"] == "file_purpose":
            return _trash_staged(s)
        return "✅ 已取消。"

    if pd["type"] == "box_conflict":
        if plain.startswith("1") or "更新" in plain:
            s["pending"] = None
            rec, err = _find_one(pd["code"])
            if err:
                return err
            return _write_fields(user, rec, {"箱号": pd["new_box"]})
        if plain.startswith("2") or "查" in plain:
            s["pending"] = None
            return "🔍 请重新发送要查询的客编或箱号。"
        if plain.startswith("3") or "展示" in plain or "信息" in plain:
            s["pending"] = None
            rec, err = _find_one(pd["code"])
            return err or ("🔍 " + pd["code"] + "：\n" + store.format_record(rec))
        return "❓ 请回复 1（更新）/ 2（重新查询）/ 3（展示信息），或「取消」。"

    if pd["type"] == "change_code":
        if plain.startswith("1") or "确认" in plain:
            s["pending"] = None
            return _commit_change_code(user, pd)
        s["pending"] = None
        return "✅ 已取消修改。"

    if pd["type"] == "swap_code":
        if plain.startswith("1") or "确认" in plain:
            s["pending"] = None
            return _commit_swap_code(user, pd)
        s["pending"] = None
        return "✅ 已取消交换。"

    if pd["type"] == "code_letter_diff":
        inp = pd["input"]
        cands = pd["candidates"] or []
        # 2 = 仅查看库内记录
        if plain.startswith("2") or "查看" in plain:
            s["pending"] = None
            lines = [f"🔍 库内与 {inp} 数字段一致、到站字母不同的记录："]
            for c in cands:
                rec, err = _find_one(c)
                lines.append(err or ("· " + store.format_record(rec)))
            return "\n".join(lines)
        # 1 = 更新库内客编为输入值
        if plain.startswith("1") or "更新" in plain:
            s["pending"] = None
            if len(cands) == 1:
                # 复用改客编提交：把该记录客编重命名为输入值（含飞书双写 + 可撤销）
                return _commit_change_code(user, {"code": cands[0], "new": inp})
            lst = "\n".join(f"· {i+1}. {c}" for i, c in enumerate(cands))
            return (f"⚠ 库内有 {len(cands)} 条数字段相同、字母不同的记录，请指定要更新的那条，例如：\n"
                    f"改客编 <旧客编> {inp}\n{lst}")
        # 3 / 取消 / 其它
        s["pending"] = None
        return "✅ 已取消。"

    if pd["type"] == "file_purpose":
        # 等待用户说明文件用途
        if "发邮件" in text and p["code"]:
            s["pending"] = None
            return _start_mail(user, s, p, extra_attachments=s["staged"], clear_staged=True)
        if (p["intent"] == "archive" or "存" in text) and p["code"]:
            s["pending"] = None
            sub = p.get("sub") or ("报放" if "报放" in text else "随车")
            return _save_staged(user, s, p["code"], sub)
        if "发错" in text or "取消" in plain:
            s["pending"] = None
            return _trash_staged(s)
        return ("❓ 请告诉我文件用途：\n"
                "· 存档 客编 [随车/报放]\n· 发邮件 客编\n· 取消（发错了）")

    s["pending"] = None
    return None   # 交回主流程


# ==================== 文件入口 ====================

def handle_file(user, path):
    """公开入口：处理完落盘会话（重启不丢）。"""
    try:
        return _handle_file_impl(user, path)
    finally:
        _save_sessions()


def _handle_file_impl(user, path):
    """收到文件（已下载到本地暂存），返回回复文本。"""
    s = _sess(user)
    name = os.path.basename(path)

    if not _known(user):
        files.trash_file(path)
        return "👋 你还未绑定身份，文件未保存。请先回复：绑定 你的姓名"

    # 草稿模式 → 文件即附件
    if s["draft"]:
        if s["replace_idx"] is not None:
            idx = s["replace_idx"]
            s["replace_idx"] = None
            if 1 <= idx <= len(s["draft"]["attachments"]):
                old = s["draft"]["attachments"][idx - 1]
                s["draft"]["attachments"][idx - 1] = path
                return (f"🔁 已把附件 {idx}（{os.path.basename(old)}）替换为 {name}\n\n"
                        + mailer.preview(s["draft"]))
        s["draft"]["attachments"].append(path)
        return f"📎 已添加附件：{name}\n\n" + mailer.preview(s["draft"])

    # 批量存档模式 → 自动暂存
    if s["batch"]:
        s["staged"].append(path)
        return f"📥 已暂存 {name}（第 {len(s['staged'])} 个）。发完请回复「完毕」。"

    # 普通模式 → 暂存并询问用途
    s["staged"].append(path)
    s["pending"] = {"type": "file_purpose"}
    return (f"📥 已收到文件：{name}（共暂存 {len(s['staged'])} 个）\n"
            f"请回复用途：\n· 存档 客编 [随车/报放]\n· 发邮件 客编\n· 取消（发错了）")


def _start_archive(user, s, p):
    """「存档 客编」：有暂存文件则直接落地，否则进入批量模式。"""
    rec, err = _find_one(p["code"])
    if err:
        return err
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)
    sub = p.get("sub") or "随车"
    if s["staged"]:
        return _save_staged(user, s, p["code"], sub)
    s["batch"] = {"code": rec["客户编码"], "sub": sub}
    return (f"📂 进入批量存档模式：{rec['客户编码']} / {sub}\n"
            f"请连续发送文件，发完回复「完毕」，中途可回复「取消」。")


def _save_staged(user, s, code, sub):
    rec, err = _find_one(code)
    if err:
        return err
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)
    saved = []
    for path in s["staged"]:
        if os.path.isfile(path):
            saved.append(files.save_file(path, rec, sub))
    s["staged"] = []
    store.push_op(user, "file_save", {"paths": saved, "code": rec["客户编码"]})
    d = files.archive_dir(rec, sub)
    return (f"✅ 已存入 {rec['客户编码']} / {sub}（{len(saved)} 个文件）\n"
            f"目录：{d}\n" + "\n".join("· " + os.path.basename(x) for x in saved)
            + "\n（回复「撤销」可移回收区）")


def _batch_finish(user, s):
    batch = s["batch"]
    s["batch"] = None
    if not s["staged"]:
        return "ℹ 批量模式结束，没有收到文件。"
    return _save_staged(user, s, batch["code"], batch["sub"])


def _batch_cancel(s):
    s["batch"] = None
    return _trash_staged(s)


def _trash_staged(s):
    n = 0
    for path in s["staged"]:
        if os.path.isfile(path):
            files.trash_file(path)
            n += 1
    s["staged"] = []
    return f"🗑 已取消，{n} 个暂存文件移入回收区。"


# ==================== 邮件 ====================

def _start_mail(user, s, p, extra_attachments=None, clear_staged=False):
    if not p["code"]:
        return "❓ 请带上客编，例如：发邮件 CQWLJT260819001-D"
    rec, err = _find_one(p["code"])
    if err:
        return err
    if not _has_perm(user, rec):
        return _perm_denied(user, rec)

    _, folder_files = files.list_folder(rec, "随车")
    d = files.archive_dir(rec, "随车")
    attachments = [os.path.join(d, f) for f in folder_files]
    if extra_attachments:
        attachments += [x for x in extra_attachments if os.path.isfile(x)]
    if clear_staged:
        s["staged"] = []

    s["draft"] = mailer.build_draft(rec, user, attachments)
    note = "" if attachments else "\n⚠ 当前没有任何附件，可直接发文件添加，或先存档随车资料。"
    return mailer.preview(s["draft"]) + note


def _draft_command(user, s, text, p):
    """草稿模式下的指令。返回 None 表示不是草稿指令，交回主流程。"""
    d = s["draft"]
    plain = text.replace(" ", "")

    if plain in ("取消", "取消邮件", "放弃"):
        s["draft"] = None
        s["replace_idx"] = None
        return "🗑 草稿已取消（文件未删除）。"

    if plain in ("确认发送", "确认", "发送"):
        ok, err = mailer.send(d)
        if not ok:
            return f"❌ 发送失败：{err}\n草稿保留，可修改后重新「确认发送」。"
        s["draft"] = None
        # 发送成功 → 随车=已发邮件（双写）
        rec, _err = _find_one(d["code"])
        status_note = ""
        if rec:
            old, _ = store.update_record(rec["id"], {"随车": "已发邮件"}, user)
            # 飞书双写（已弃用，本地库为准）
            fs_ok, fs_rid, fs_old, fs_err = True, None, {}, ""
            store.push_op(user, "db_update", {
                "rec_id": rec["id"], "code": d["code"],
                "fields": {"随车": "已发邮件"}, "old": old,
                "fs_record_id": fs_rid, "fs_old": fs_old, "fs_ok": fs_ok})
            status_note = f"\n随车状态 → 已发邮件（本地 ✅ | 飞书 {'✅' if fs_ok else '❌'}）"
            # 把暂存区来的附件归档进随车文件夹
            arch_dir = files.archive_dir(rec, "随车")
            moved = 0
            for pth in d["attachments"]:
                if os.path.isfile(pth) and not pth.startswith(arch_dir):
                    try:
                        files.save_file(pth, rec, "随车")
                        moved += 1
                    except Exception:
                        pass
            if moved:
                status_note += f"\n📂 {moved} 个新附件已归档到随车文件夹"
        store.push_op(user, "mail_sent", {"code": d["code"], "subject": d["subject"]})
        return (f"📨 邮件已发送 ✅\n主题：{d['subject']}\n"
                f"收件：{', '.join(d['to'])}{status_note}")

    m = re.match(r"^更新正文\s*([\s\S]+)$", text)
    if m:
        d["body"] = m.group(1).strip()
        return "✏ 正文已更新\n\n" + mailer.preview(d)

    m = re.match(r"^(更新收件|更新收件人|改收件)\s*(.+)$", text)
    if m:
        emails = re.findall(r"[\w.\-]+@[\w.\-]+", m.group(2))
        if not emails:
            return "❓ 没识别到邮箱地址，请重发，例如：更新收件 abc@xx.com def@xx.com"
        d["to"] = emails
        return "✏ 收件人已更新\n\n" + mailer.preview(d)

    m = re.match(r"^(更新抄送|改抄送)\s*(.*)$", text)
    if m:
        emails = re.findall(r"[\w.\-]+@[\w.\-]+", m.group(2))
        d["cc"] = emails
        return "✏ 抄送已更新\n\n" + mailer.preview(d)

    m = re.match(r"^删除附件\s*(\d+)$", plain.replace("删除附件", "删除附件 ")) or \
        re.match(r"^删除附件(\d+)$", plain)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(d["attachments"]):
            removed = d["attachments"].pop(idx - 1)
            return f"🗑 已移除附件 {idx}：{os.path.basename(removed)}\n\n" + mailer.preview(d)
        return f"❓ 附件序号超出范围（当前 {len(d['attachments'])} 个）。"

    m = re.match(r"^替换附件(\d+)$", plain)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(d["attachments"]):
            s["replace_idx"] = idx
            return f"🔁 好的，请发送新文件来替换附件 {idx}（{os.path.basename(d['attachments'][idx-1])}）。"
        return f"❓ 附件序号超出范围（当前 {len(d['attachments'])} 个）。"

    if "查看文件夹" in plain or plain == "文件夹":
        rec, err = _find_one(d["code"])
        if err:
            return err
        _dir, names = files.list_folder(rec, "随车")
        if not names:
            return "📂 随车文件夹是空的。"
        lines = ["📂 随车文件夹文件（回复「加 N」添加为附件）："]
        lines += [f"  {i}. {n}" for i, n in enumerate(names, 1)]
        return "\n".join(lines)

    m = re.match(r"^加\s*(\d+)$", plain.replace("加", "加 ", 1)) or re.match(r"^加(\d+)$", plain)
    if m:
        rec, err = _find_one(d["code"])
        if err:
            return err
        _dir, names = files.list_folder(rec, "随车")
        idx = int(m.group(1))
        if 1 <= idx <= len(names):
            path = os.path.join(_dir, names[idx - 1])
            if path in d["attachments"]:
                return f"ℹ {names[idx-1]} 已在附件里了。"
            d["attachments"].append(path)
            return f"📎 已添加：{names[idx-1]}\n\n" + mailer.preview(d)
        return f"❓ 序号超出范围（文件夹共 {len(names)} 个文件）。"

    if plain in ("预览", "查看草稿", "草稿"):
        return mailer.preview(d)

    return None   # 不是草稿指令


# ==================== 文件夹查看（非草稿态） ====================

def _list_folder_cmd(p):
    if not p["code"]:
        return "❓ 请带上客编，例如：查看文件夹 CQWLJT260819001-D"
    rec, err = _find_one(p["code"])
    if err:
        return err
    lines = []
    for sub in ("随车", "报放"):
        d, names = files.list_folder(rec, sub)
        lines.append(f"📂 {sub}（{len(names)} 个）")
        lines += [f"  {i}. {n}" for i, n in enumerate(names, 1)]
    return f"{rec['客户编码']} 的归档：\n" + "\n".join(lines)
