# -*- coding: utf-8 -*-
"""
WeComBot unified config center.
All credentials and paths are managed here; other modules import from this file.

⚠ 敏感凭证（企微/飞书密钥、SMTP 密码）已外置到同目录 secrets.json，
  本文件不再保存任何明文密码。secrets.json 切勿分享或提交代码仓库。
  （2026-07-29 芙蕾雅改造，原版备份在 backups/2026-07-29_芙蕾雅/）
"""
import os as _os
import json as _json

_SECRETS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "secrets.json")
try:
    with open(_SECRETS_PATH, "r", encoding="utf-8") as _f:
        _SECRETS = _json.load(_f)
except FileNotFoundError:
    raise RuntimeError(
        f"缺少凭证文件 {_SECRETS_PATH}。"
        "请参照 secrets.example.json 创建并填入真实密钥。")
except Exception as _e:
    raise RuntimeError(f"凭证文件 {_SECRETS_PATH} 解析失败：{_e}")

# ==================== WeCom (Enterprise WeChat) credentials ====================
CORP_ID = _SECRETS["CORP_ID"]
AGENT_ID = _SECRETS["AGENT_ID"]
SECRET = _SECRETS["SECRET"]

# Callback config (Admin console -> Self-built app -> Receive messages)
TOKEN = _SECRETS["TOKEN"]
ENCODING_AES_KEY = _SECRETS["ENCODING_AES_KEY"]

# 微信客服可独立配置 Secret；若空则复用上面的 SECRET
KF_SECRET = _SECRETS.get("KF_SECRET", "")

# ==================== Mail config ====================
SMTP_SERVER = "smtp.qiye.aliyun.com"
SMTP_PORT = 465

# 邮箱 -> SMTP 密码（已外置到 secrets.json）
ACCOUNTS = _SECRETS["ACCOUNTS"]

# Tracing mail default sender filter
SENDER_FILTER = "tracing-system@yxologistics.com"

# DSK mail default sender filter
DSK_SENDER_FILTER = "kasa@rtsb.de"

# 草单/运单号邮件自动转发（来自渝新欧 youlia 等）
DRAFT_WAYBILL_SENDER_FILTER = "youlia@yxologistics.com"
DRAFT_WAYBILL_FOLDER = "运单草单"          # IMAP 文件夹名（中文）
DRAFT_WAYBILL_IMAP_FOLDER = "&j9BTVYNJU1U-"  # 上述文件夹的 IMAP modified-UTF7 编码

# auto_manager default recipients
DEFAULT_TO = "luohua@yxologistics.com"
DEFAULT_CC = "documents-wb@yxologistics.com"

# DSK default recipients (when not using tracing default)
DSK_DEFAULT_TO = DEFAULT_TO
DSK_DEFAULT_CC = DEFAULT_CC

# ==================== Local paths ====================
YXO_DATA_ROOT = r"D:\YXO_DATA"
BOT_LOG_DIR = r"D:\YXO_DATA\WeComBot\logs"
BOT_TEMP_DIR = r"D:\YXO_DATA\WeComBot\temp"

# ==================== Service config ====================
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5001

# ==================== CS Bot (customer service bot) ====================
# YXO booking sqlite db
YXO_DB_PATH = r"D:\YXO_DATA\yxo_app\data\yxo.db"
# Archive root: D:\YXO_DATA\YXO_DATA\2026\{MM}\{M.D 班列号}\{客编}_{箱号}\{随车|报放}
ARCHIVE_ROOT = r"D:\YXO_DATA\YXO_DATA"
# cs_bot local state db (undo history / bindings / staging)
CS_BOT_DB = r"D:\YXO_DATA\WeComBot\data\cs_bot.db"
# staged files dir (before purpose is confirmed)
CS_STAGING_DIR = r"D:\YXO_DATA\WeComBot\data\staging"
# soft-delete trash dir (undo of file saves)
CS_TRASH_DIR = r"D:\YXO_DATA\WeComBot\data\trash"

# WeCom UserID -> real name
WECOM_USER_MAP = {
    "MaoXiaoYang": "毛骁洋",
    "wulala": "杨雅雯",      # 2026-07-29 通讯录核实（绑定表 14:58 杨雅雯→wulala）
    "BanXian": "冯茜",       # 2026-07-29 通讯录核实
    "HanWenHao": "韩文豪",   # 2026-07-29 通讯录核实
}

# real name -> managed company keywords (substring match on 开票子公司名称)
USER_COMPANIES = {
    "毛骁洋": ["太平洋", "港九港铁"],
    "杨雅雯": ["同程配", "东盟"],
    "冯茜": ["保时达"],
    "韩文豪": ["沙坪坝", "中欧木业"],
}

# 反向查找：公司名 -> 负责人企微 UserID（用于邮件转发后自动通知）
# 匹配规则：只要公司名包含上述关键词，就归到对应负责人
_COMPANY_KEYWORDS = []
for _real_name, _keywords in USER_COMPANIES.items():
    for _kw in _keywords:
        _COMPANY_KEYWORDS.append((_kw, _real_name))


def company_to_name(company_name):
    """根据公司名称返回负责同事的真名；找不到返回 None。
    通知请用 wecom_api.notify_by_name(真名, 文本)——走微信客服通道，微信直接可收。"""
    if not company_name:
        return None
    c = str(company_name)
    for kw, real_name in _COMPANY_KEYWORDS:
        if kw in c:
            return real_name
    return None


def company_to_user(company_name):
    """（已废弃，仅兼容保留）根据公司名称返回负责同事的企微 UserID。
    ⚠ 企微通讯录里只有 MaoXiaoYang 一个成员，其他人的 UserID 是无效的（81013），
    不要再用它发通知，改用 company_to_name + wecom_api.notify_by_name。"""
    real_name = company_to_name(company_name)
    if real_name == "毛骁洋":
        return "MaoXiaoYang"
    return None


# admins bypass company permission
ADMIN_USERS = ["毛骁洋"]

# real name -> sender email (password in ACCOUNTS)
USER_EMAILS = {
    "毛骁洋": "maoxiaoyang@cqtransit.com",
    "杨雅雯": "yangyawen@cqtransit.com",
    "冯茜": "fengqian@cqtransit.com",
    "韩文豪": "hanwenhao@cqtransit.com",
}

# 公司关键词 -> 负责同事的发件邮箱（用于转发/发送时以对应同事身份发信）
COMPANY_TO_EMAIL = {}
for _real_name, _keywords in USER_COMPANIES.items():
    _email = USER_EMAILS.get(_real_name)
    for _kw in _keywords:
        COMPANY_TO_EMAIL[_kw] = _email


def sender_for_company(company_name):
    """返回 (email, password) 供 SMTP 以对应同事身份发信；找不到返回 None。

    用途：ATB/DSK 邮件转发、订舱助手手动发邮件，都按箱号所属公司的负责同事
    来发信（收件人看到的是该同事的邮箱，而不是默认账号）。
    """
    if not company_name:
        return None
    c = str(company_name)
    for kw, email in COMPANY_TO_EMAIL.items():
        if kw in c:
            return email, ACCOUNTS.get(email)
    return None


# option values for the three status fields (write options only, no free text)
FIELD_OPTIONS = {
    "草单": ["未出", "已确认", "欧线"],
    "报放单": ["未出", "已上传"],
    "随车": ["未收", "已发邮件"],
}
