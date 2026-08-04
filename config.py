# -*- coding: utf-8 -*-
"""
YXO 订舱数据管理 · 配置文件
只在这里改"业务参数"，代码不用动。
字段类型与选项 2026-07-27 从飞书总表(tbl73fJJQmk4S8ly)在线查询对齐。
"""
import os

# 项目根目录 / 数据目录 / 数据库文件
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "yxo.db")

# 内部监听端口（由 nginx 的 /yxo/ 反代，无需新增公网端口）
PORT = 5011

# Excel 数据源（首次启动 / 点"导入"时读取）
IMPORT_FILE = r"D:\YXO_DATA\output\八月记录汇总.xlsx"

# 价格配置（自动算价用，复用现有 price_config.json）
PRICE_CONFIG = r"D:\YXO_DATA\MailBots\price_config.json"

# 4 位同事（无登录；仅用于"个人筛选互不影响"的标识）。
USERS = ["毛骁洋", "冯茜", "杨雅雯", "韩文豪"]

# 托书生成权限：独立分组，与系统管理的受限管理员(LIMITED_ADMINS)不是一回事。
TUOSHU_ADMINS = ["毛骁洋", "杨雅雯"]

# —— 字段定义（列顺序 = 飞书总表列顺序，同事零学习成本）——
# kind: base=Excel 导入的元数据 / op=同事日常维护 / bot=机器人自动写入（也可手动补录）
# type: text / select / date / number    select 的 options 与飞书总表选项一致
# 可选扩展属性：
#   options      : 单选选项（与飞书一致）；值存于数据库 field_options 表，可在界面"选项维护"里增删
#   maintainable : True 表示选项可在界面里由用户维护（增/删）
#   free_text    : True 表示该字段"可手输 + 根据输入筛选选项"（目的站，并非所有站点都预置）
#   default      : 新增行时空值套用的默认值（已存在的空单元格也会在升级时一次性补填）
FIELD_DEFS = [
    # 班列类型：散舱 / 专列（专列流程不同于散舱，需分开展示与维护；前端用类型切换 tab 表达，故不显示该列）
    {"name": "班列类型",       "kind": "base", "type": "select", "options": ["散舱", "专列"],
     "maintainable": True, "default": "散舱", "internal": True},
    {"name": "客户编码",       "kind": "base", "type": "text"},
    {"name": "箱号",           "kind": "op",   "type": "text"},
    {"name": "封号",           "kind": "op",   "type": "text"},
    {"name": "箱属",           "kind": "op",   "type": "select", "options": ["SOC", "COC"],
     "maintainable": True, "default": "SOC"},
    {"name": "发班时间",       "kind": "base", "type": "date"},
    {"name": "台账月份",       "kind": "op",   "type": "select", "free_text": True,
     "hint": "决定该记录在“月份标签”里归属哪个月（默认随发班时间，可手动改成 2026-07 等，不影响发班时间与价格）"},
    {"name": "班列号",         "kind": "base", "type": "text"},
    {"name": "口岸",           "kind": "base", "type": "select",
     "options": ["山口", "满洲里", "果斯", "二连浩特"], "maintainable": True},
    {"name": "目的站",         "kind": "base", "type": "select",
     "options": ["电煤", "科里亚季奇", "杜伊斯堡", "杜伊斯堡（时刻表）", "沃尔西诺", "布达佩斯",
                 "别雷拉斯特", "马拉", "谢利亚季诺", "克列西哈", "叶卡捷琳堡", "马拉（时刻表）",
                 "谢丽亚基诺", "明斯克", "莫斯科", "满洲里", "罗斯托夫"],
     "maintainable": True, "free_text": True},
    {"name": "入堆场",         "kind": "op",   "type": "date"},
    {"name": "入站",           "kind": "op",   "type": "date"},
    {"name": "随车",           "kind": "op",   "type": "select", "options": ["未收", "已收", "已发邮件"],
     "maintainable": True, "default": "未收"},
    {"name": "草单",           "kind": "op",   "type": "select", "options": ["未出", "已出", "已确认"],
     "maintainable": True, "default": "未出"},
    {"name": "报放单",         "kind": "op",   "type": "select", "options": ["未出", "已出", "已上传"],
     "maintainable": True, "default": "未出"},
    {"name": "开票子公司名称", "kind": "base", "type": "select", "label": "负责公司",
     "options": ["港九港铁", "保时达", "中欧木业", "同程配", "沙坪坝", "太平洋", "东盟"], "maintainable": True},
    {"name": "货源类型",       "kind": "op",   "type": "select", "options": ["绕园", "本地", "外地"],
     "maintainable": True},
    {"name": "本地货源公司",   "kind": "op",   "type": "text"},
    {"name": "状态",           "kind": "op",   "type": "select", "options": ["正常", "退舱", "延期"],
     "maintainable": True, "default": "正常", "trainTypes": ["散舱", "全部"]},
    {"name": "专列状态",        "kind": "op",   "type": "select", "options": ["正常", "非正常"],
     "maintainable": False, "default": "正常", "trainTypes": ["专列", "全部"]},
    {"name": "dsk",            "kind": "bot",  "type": "text", "hint": "DSK机器人自动写入，可手动补录"},
    {"name": "ATB",            "kind": "bot",  "type": "text", "hint": "ATB机器人自动写入，可手动补录"},
    {"name": "单箱价格",       "kind": "op",   "type": "number"},
    {"name": "备注",           "kind": "op",   "type": "text"},
]

# 从 Excel 导入的基础字段（导入时按此刷新已存在行）
BASE_FIELDS = [f["name"] for f in FIELD_DEFS if f["kind"] == "base"]
# 同事日常维护的操作字段 + 机器人字段
OP_FIELDS = [f["name"] for f in FIELD_DEFS if f["kind"] != "base"]

# 公司字段（用于"多选公司"筛选，满足一人看多家客户）
COMPANY_FIELD = "开票子公司名称"

# 看板可以按这些字段分组
GROUPABLE = ["状态", "专列状态", "开票子公司名称", "口岸", "目的站", "箱属", "货源类型", "随车", "草单", "报放单"]

# "待跟进"判定：这些字段为空 → 算作待跟进
FOLLOWUP_FIELDS = ["状态", "草单", "报放单"]

# 全部字段（数据库列顺序 = 显示顺序）
ALL_FIELDS = [f["name"] for f in FIELD_DEFS]

# ==================== 舱单导入配置（2026-08-04 芙蕾雅《舱单统一导入总体设计》）====================
# 口岸别名：舱单里写「阿拉山口」，库里存「山口」。识别别名 → 库标准写法（写库值）。
# 匹配不上任何别名 → 不写，列入报警清单。新增口岸只改这里，不动引擎逻辑。
PORT_ALIASES = {
    "山口": ["阿拉山口", "山口", "阿拉", "阿拉山口站"],
    "果斯": ["霍尔果斯", "果斯", "霍尔", "霍尔果斯站"],
    "满洲里": ["满洲里", "满洲里站"],
    "二连浩特": ["二连浩特", "二连", "二连站"],
}
# 反向索引：别名(大写/去空格) → 标准写法，供引擎快速查
PORT_ALIAS_LOOKUP = {}
for _std, _alts in PORT_ALIASES.items():
    PORT_ALIAS_LOOKUP[_std.upper()] = _std
    for _a in _alts:
        PORT_ALIAS_LOOKUP[_a.strip().upper()] = _std

# 后缀 → 目的站 建议映射（仅作提示，不自动写；一对多不干净，骁洋定）。
# 客户编码形如 CQWLJT260730001-VXN，后缀即「-VXN」。
SUFFIX_DEST_MAP = {
    "-DMZ": "电煤", "-KOL": "科里亚季奇", "-BLLST": "别雷拉斯特", "-MA": "马拉",
    "-D": "杜伊斯堡", "-XLYJN": "谢利亚季诺", "-VXN": "沃尔西诺", "-EKA": "叶卡捷琳堡",
    "-MOS": "沃尔西诺", "-SPB": "圣彼得堡 L站/V站", "-BUD": "布达佩斯",
    "-KLXH": "克列西哈", "-SBN": "沙巴内",
}

# 舱单导入可写字段白名单（其余字段一律不写，含设计文档 1.1 那七个排除字段）。
MANIFEST_WRITABLE = ["班列号", "口岸", "发班时间", "箱号", "封号", "箱属", "目的站"]

# 舱单导入入口仅毛骁洋可见可用（设计文档 10.2，比受限管理员更严格）。
MANIFEST_ADMIN = "毛骁洋"
