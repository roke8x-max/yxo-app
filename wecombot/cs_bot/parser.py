# -*- coding: utf-8 -*-
"""
宽容指令解析器：先抽实体（客编/箱号/字段/选项值），再判意图。
支持非标准格式，如：
  CQWLJT260819001-D更新箱号TCLU1234567
  更新CQWLJT260819001-D箱号：TCLU1234567
  CQWLJT260819001-D 草单 已确认
  CQWLJT260819001-D TCLU1234567          （歧义规则由 engine 处理）
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import FIELD_OPTIONS

# 客编: 如 CQWLJT260808001-VXN（字母段+数字段+短横+后缀）
CODE_RE = re.compile(r"[A-Z]{2,8}\d{6,12}-[A-Za-z0-9]+")
# 标准集装箱号: 4字母+7数字（前后可紧贴中文，不能用 \b）
BOX_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}\s?\d{7})(?!\d)")
# 班列号: WB+数字（如 WB759）
TRAIN_RE = re.compile(r"(?<![A-Z0-9])(WB\s?\d{2,5})(?!\d)")

FIELD_ALIASES = {
    "草单": "草单",
    "报放单": "报放单", "报放": "报放单",
    "随车": "随车",
    "箱号": "箱号",
    "封号": "封号",
    "备注": "备注",
}

UPDATE_WORDS = ("更新", "增加", "修改", "改为", "改成", "改", "加", "设置", "设为", "写")
QUERY_WORDS = ("查询", "查", "搜索", "搜", "看看", "查一下")


def normalize(text):
    """全角转半角、去多余空白。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip()


def parse_months(text):
    """从文本中解析月份，返回 ['YYYY/MM', ...]。
    支持：8月 / 7-8月 / 7月-8月 / 7~8月 / 2026年8月 / 2025年12-2月（跨年）
    """
    import datetime as _dt
    text = normalize(text)
    year_m = re.search(r"(\d{4})\s*年", text)
    base_year = int(year_m.group(1)) if year_m else _dt.date.today().year

    months = []
    # 先找范围：7-8月 / 7月-8月 / 7~8月 / 7到8月 / 7至8月
    mr = re.search(r"(\d{1,2})\s*月?\s*[-~～到至]\s*(\d{1,2})\s*月", text)
    if mr:
        a, b = int(mr.group(1)), int(mr.group(2))
        if 1 <= a <= 12 and 1 <= b <= 12:
            y = base_year
            m = a
            while True:
                months.append(f"{y}/{m:02d}")
                if m == b and (a <= b or y > base_year):
                    break
                m += 1
                if m > 12:
                    m = 1
                    y += 1
                if len(months) > 12:  # 防呆
                    break
            return months
    # 单月（可能出现多个，如 "统计 7月 9月"）
    for mm in re.finditer(r"(\d{1,2})\s*月", text):
        v = int(mm.group(1))
        if 1 <= v <= 12:
            key = f"{base_year}/{v:02d}"
            if key not in months:
                months.append(key)
    return months


def parse(text):
    """
    返回 dict:
      intent: undo/bind/help/mail/list_folder/update/query/ambiguous/unknown
      code, box, fields{字段:值}, name(绑定), raw
    """
    raw = text
    text = normalize(text)
    up = text.upper()

    r = {"intent": "unknown", "code": None, "box": None,
         "fields": {}, "name": None, "sub": None, "train": None, "raw": raw}

    # --- 简单指令 ---
    plain = text.replace(" ", "")
    if plain in ("撤销", "撤回", "UNDO", "undo"):
        r["intent"] = "undo"
        return r
    if plain in ("帮助", "HELP", "help", "?", "？", "菜单"):
        r["intent"] = "help"
        return r
    # 「？！」查询当前环节（normalize 后全角已转半角）
    if plain in ("?!", "!?", "状态", "我在哪", "在哪一步", "现在在哪"):
        r["intent"] = "status"
        return r
    # 返回主页：立即放弃当前进行中的操作
    if plain in ("主页", "返回主页", "回主页", "回到主页", "首页", "主菜单", "退出", "重置", "回首页"):
        r["intent"] = "home"
        return r
    m = re.match(r"^绑定\s*(\S{2,10})$", text)
    if m:
        r["intent"] = "bind"
        r["name"] = m.group(1)
        return r

    # --- 统计（如：统计 8月 太平洋 货源 / 统计 7-8月 太平洋 保时达 货源）---
    if "统计" in text:
        r["intent"] = "stats"
        r["stat_months"] = parse_months(text)
        return r

    # --- 改客编 / 交换客编（需两个客编，单个客编不拦截普通更新）---
    _cc_kw = re.search(r'(改客编|修改客编|交换客编|互换客编|客编交换|客编改为|客编改成)', text)
    _cc_soft = ("客编" in text) and bool(re.search(r'(改为|改成|变更|换成)', text))
    if _cc_kw or _cc_soft:
        _codes = CODE_RE.findall(up)
        if len(_codes) >= 2:
            _is_swap = bool(re.search(r'交换客编|互换客编|客编交换', text))
            r["intent"] = "swap_code" if _is_swap else "change_code"
            r["code"] = _codes[0]
            r["code2"] = _codes[1]
            return r
        # 只有 1 个客编：更可能是普通字段更新（如“客编X 草单改为已确认”），不拦截

    # --- 抽实体 ---
    m = CODE_RE.search(up)
    if m:
        # 从原文中取回原始大小写（客编后缀可能小写）
        r["code"] = text[m.start():m.end()]
    boxes = [b.replace(" ", "") for b in BOX_RE.findall(up)]
    # 箱号不能和客编前缀重叠
    if r["code"]:
        code_up = r["code"].upper()
        boxes = [b for b in boxes if b not in code_up]
    if boxes:
        r["box"] = boxes[0]
    mt = TRAIN_RE.search(up)
    if mt:
        t = mt.group(1).replace(" ", "")
        # 班列号不能与客编/箱号内容重叠
        if not (r["code"] and t in r["code"].upper()) and not any(t in b for b in boxes):
            r["train"] = t

    # --- 发邮件 ---
    if "发邮件" in text or "发送邮件" in text:
        r["intent"] = "mail"
        return r

    # --- 存档（优先于字段更新，避免「存档 xx 报放」被误判） ---
    if re.search(r"存档|存文件|存到|归档", text) or re.match(r"^存\s", text):
        r["intent"] = "archive"
        r["sub"] = "报放" if "报放" in text else "随车"
        return r

    # --- 查看文件夹 ---
    if "查看文件夹" in text or "文件夹" in text and any(w in text for w in QUERY_WORDS):
        r["intent"] = "list_folder"
        return r
    if "查看文件夹" in plain or plain.startswith("文件夹"):
        r["intent"] = "list_folder"
        return r

    # --- 字段更新：找 字段名 + 值 ---
    rest = text
    if r["code"]:
        rest = rest.replace(r["code"], " ")
    fields = {}
    for alias, field in FIELD_ALIASES.items():
        if alias not in rest:
            continue
        if field in ("草单", "报放单", "随车"):
            # 值必须是数据库选项
            for opt in FIELD_OPTIONS[field]:
                if opt in rest:
                    fields[field] = opt
                    break
            else:
                # 提到了字段但没给合法选项值 → engine 反问
                fields[field] = None
        elif field == "箱号":
            if r["box"]:
                fields["箱号"] = r["box"]
            elif any(w in rest for w in ("删除", "清空", "清除")):
                fields["箱号"] = ""
        elif field in ("封号", "备注"):
            m2 = re.search(rf"{alias}[:：]?\s*(\S.*?)$", rest)
            if m2:
                val = m2.group(1).strip()
                # 去掉夹在值里的更新动词
                for w in UPDATE_WORDS:
                    if val.startswith(w):
                        val = val[len(w):].strip()
                if val:
                    fields[field] = val

    has_update_word = any(w in rest for w in UPDATE_WORDS)

    # 有客编 + 有箱号 + 出现更新动词但没提"箱号"二字 → 也视为更新箱号
    if r["code"] and r["box"] and has_update_word and "箱号" not in fields:
        fields["箱号"] = r["box"]

    if fields:
        r["intent"] = "update"
        r["fields"] = fields
        return r

    # --- 纯查询 ---
    has_query_word = any(w in text for w in QUERY_WORDS)
    if r["code"] and r["box"]:
        r["intent"] = "ambiguous"     # 歧义规则交给 engine（对照库里箱号）
        return r
    if r["train"] and not r["code"] and not r["box"]:
        r["intent"] = "train_query"   # 班列号查询
        return r
    if r["code"] or r["box"]:
        r["intent"] = "query"
        return r
    if has_query_word:
        r["intent"] = "query"
        return r

    return r
