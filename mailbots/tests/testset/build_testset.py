# -*- coding: utf-8 -*-
"""
邮件机器人重构 · 测试集生成器（芙蕾雅 2026-08-26，拆分专项扩展）
产出:
  test_fixtures/
    records.csv              <- 匹配用的 records 基准数据（含退舱/软删/复用等边界）
    draft_nums_seed.txt      <- B类判定需要的"已转过草单"数字段台账种子
    draft/*.eml              <- 草单类 17 例 (A/B/C1/C2/W + T0~T7 + 边界)
    waybill/*.eml            <- 运单号类 9 例 (WAY_A 拆分 + 多行/.xls 拆分专项)
    tracing/*.eml            <- 运踪类 3 例
    dsk/*.eml                <- DSK 类 3 例
    atb/*.eml                <- ATB 类 6 例 (逐箱号拆分专项)
    manifest.json            <- 每条夹具的预期 {type, category, tier, action, note, split?, forward?}
说明:
  - 附件仅用文件名参与判定（机器人靠文件名正则识别草单/箱号），正文/附件内容用占位即可。
  - 运踪 xls 机器人不解析只转发，附件为占位 .xls。
  - DSK/ATB 的箱号在 HTML 正文，正文用贴近真实的表格。
  - 运单号 WAY_A 拆分型夹具(W06~W09) 用 openpyxl 生成真实多行 .xlsx，使解析+拆分+转发可端到端验证。
  - 拆分型夹具在 manifest 里额外给出结构化 split(拆分结果) 与 forward(转发结果) 预期，
    opencode 重构后应能断言：拆成几封、每封含哪些行/附件、发给谁、哪些行报警/跳过。
  - 现状提示（写进文档）：当前 Waybill_Robot.run() 只取 xls rows[0]，且 build_forward 整封原样，
    并未真正"按公司拆多行+生成小 xls"；本测试集的预期是「重构后目标行为」。
"""
import os, csv, json, io, email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.header import Header
from email.utils import formataddr, make_msgid

try:
    from openpyxl import Workbook
except ImportError:
    Workbook = None

OUT = r"C:\Users\Roke8x\WorkBuddy\2026-07-29-10-11-57\test_fixtures"
MON = ["maoxiaoyang@cqtransit.com", "yangyawen@cqtransit.com",
       "fengqian@cqtransit.com", "hanwenhao@cqtransit.com"]

records = []   # dict: code, box, company, status, is_deleted
def rec(code, box, company, status="", is_deleted=0):
    records.append({"客户编码": code, "箱号": box, "开票子公司名称": company,
                    "状态": status, "is_deleted": is_deleted})

manifest = []
cases = []     # (subdir, filename, msg, expected_dict)

def add(sub, fn, msg, expected):
    cases.append((sub, fn, msg))
    manifest.append({"file": f"{sub}/{fn}", **expected})

def make_msg(subject, sender_name, sender_addr, to_list, body_html=None,
             body_text=None, attachments=None, in_reply=False):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(subject, "utf-8").encode()
    msg["From"] = formataddr((sender_name, sender_addr))
    msg["To"] = ", ".join(to_list)
    msg["Date"] = "Wed, 26 Aug 2026 01:00:00 +0800"
    msg["Message-ID"] = make_msgid(domain="yxologistics.com")
    if in_reply:
        msg["In-Reply-To"] = "<base@example.com>"
    alt = MIMEMultipart("alternative")
    if body_text:
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        alt.attach(MIMEText(body_html, "html", "utf-8"))
    if not body_text and not body_html:
        alt.attach(MIMEText("(空正文)", "plain", "utf-8"))
    msg.attach(alt)
    for att_name, att_bytes in (attachments or []):
        part = MIMEApplication(att_bytes)
        part.add_header("Content-Disposition", "attachment", filename=att_name)
        msg.attach(part)
    return msg

def pdf_bytes():
    return b"%PDF-1.4\n%test-fixture\n"

# 运单号真实多行 xlsx（openpyxl 生成，可端到端解析+拆分）
def waybill_xls(rows):
    if Workbook is None:
        # 兜底：仍生成占位，但标注（拆分测试应以 openpyxl 可用为前提）
        return b"\x50\x4b\x03\x04 test-xls-fixture"
    wb = Workbook()
    ws = wb.active
    ws.title = "运单号"
    ws.append(["客户编码", "箱号", "运单号"])
    for r in rows:
        ws.append([r.get("客户编码", ""), r.get("箱号", ""), r.get("运单号", "")])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def tracing_xls_bytes():
    return b"\x50\x4b\x03\x04 test-xls-fixture"

# =====================================================================
# 1) records 基准（同时喂养 草单匹配 / 运单号匹配 / ATB 箱号→公司映射）
# =====================================================================
# D01 全匹配
rec("CQWLJT260713004-BLLST", "TSRU8008478", "莫斯科子公司")
# D02 客编精确、库箱号为空
rec("CQWLJT260713005-SPB", "", "圣彼得堡子公司")
# D03 B更新：数字段 260709001 进 draft_nums_seed
rec("CQWLJT260709001-Kol", "OVLU2507254", "科洛姆纳子公司")
# D07 退舱（应被排除）
rec("CQWLJT260713006-VXN", "TCLU3000001", "越南子公司", status="退舱")
# D08 软删除（应被排除）
rec("CQWLJT260713007-MOS", "TCLU3000002", "莫斯科子公司", is_deleted=1)
# D09 T2：同序号同箱号异后缀(两active)
rec("CQWLJT260810001-SPB", "ABCU1111111", "圣彼得堡子公司")
rec("CQWLJT260810001-VXN", "ABCU1111111", "越南子公司")
# D10 T3：序号同箱号异
rec("CQWLJT260810002-SPB", "ABCU2222222", "圣彼得堡子公司")
# D11 T4：序号不在库（不建记录）
# D12 T5：无客编+箱号唯一
rec("CQWLJT260810003-SPB", "ABCU4444444", "圣彼得堡子公司")
# D13 T6：箱号复用 >=2
rec("CQWLJT260810004-SPB", "ABCU5555555", "圣彼得堡子公司")
rec("CQWLJT260810004-VXN", "ABCU5555555", "越南子公司")
# D14 T7：序号跨>=2 active 后缀
rec("CQWLJT260810005-SPB", "ABCU6666666", "圣彼得堡子公司")
rec("CQWLJT260810005-VXN", "ABCU6666666", "越南子公司")
# D15 T0：无客编无箱号（无记录）
# 运单号用：外部公司分组（东盟 call56 / 内部布拉茨克 / 明斯克）
rec("CQWLJT260816001-MA", "TEMU9000001", "东盟子公司")   # call56 外部
rec("CQWLJT260816002-MA", "TEMU9000002", "东盟子公司")
rec("CQWLJT260816003-BLLST", "TEMU9000003", "布拉茨克子公司")  # 内部
rec("CQWLJT260816004-MSK", "TEMU9000004", "明斯克子公司")      # 外部（A04 第三箱号）

# =====================================================================
# 2) 草单 DRAFT（17 例）
# =====================================================================
MON_TO = MON

add("draft", "D01_A_full.eml",
    make_msg("运单草单_YXO-2026-656_CQWLJT260713004-BLLST_TSRU8008478",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("TSRU8008478-260711-150827已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T1", "action": "forward",
     "note": "客编精确+箱号精确→自动转发；A因数字段260713004未进台账"})

add("draft", "D02_A_box_empty.eml",
    make_msg("运单草单_YXO-2026-657_CQWLJT260713005-SPB_XXLU0000001",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("XXLU0000001-260711-150900已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T1", "action": "forward",
     "note": "库CQWLJT260713005-SPB箱号为空，按T1客编主键仍转发（箱号只辅助）"})

add("draft", "D03_B_update.eml",
    make_msg("运单草单_YXO-2026-658_CQWLJT260709001-Kol_OVLU2507254",
             "董长丽", "dongchangli@yxologistics.com", MON_TO,
             body_text="请查收更新草单。",
             attachments=[("OVLU2507254-260707-170017已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "B", "tier": "T1", "action": "forward",
     "note": "数字段260709001已在draft_nums_seed→B；正文须带'更新'提示"})

add("draft", "D04_C1_feedback.eml",
    make_msg("关于CQWLJT260713004-BLLST箱况问题",
             "刘江兰", "liujianglan@yxologistics.com", MON_TO,
             body_text="该箱号目的站信息需确认，请回复。",
             attachments=[("InsertPic_9F93.png", b"\x89PNG\r\n test")]),
    {"type": "draft", "category": "C1", "tier": "-", "action": "forward",
     "note": "yxologistics.com且无草单附件→C1反馈，转发负责同事"})

add("draft", "D05_C2_reply.eml",
    make_msg("Re: CQWLJT260713004-BLLST 订舱确认",
             "客户代理", "agent@forwarder.eu", MON_TO,
             body_text="已确认订舱，谢谢。"),
    {"type": "draft", "category": "C2", "tier": "-", "action": "record_only",
     "note": "非yxologistics.com域且无草单附件→C2确认回复，只记录不转发"})

add("draft", "D06_W_waybill_no.eml",
    make_msg("2026-08-07 YXO-2026-781 CQWLJT运单号",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号已出，见附件。",
             attachments=[("2026-08-07 YXO-2026-781 CQWLJT运单号.xls", tracing_xls_bytes())]),
    {"type": "draft", "category": "W", "tier": "-", "action": "record_only",
     "note": "主题含'运单号'→W，只记录留给运单号模块（注意：此件若落入运单号机器人则归WAY_A）"})

add("draft", "D07_tc_excluded.eml",
    make_msg("运单草单_YXO-2026-659_CQWLJT260713006-VXN_TCLU3000001",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("TCLU3000001-260711-160000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T4", "action": "alarm",
     "note": "客编260713006-VXN记录状态=退舱，解析范围排除→未命中→T4报警"})

add("draft", "D08_softdel_excluded.eml",
    make_msg("运单草单_YXO-2026-660_CQWLJT260713007-MOS_TCLU3000002",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("TCLU3000002-260711-160100已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T4", "action": "alarm",
     "note": "客编260713007-MOS is_deleted=1，解析范围排除→未命中→T4报警"})

add("draft", "D09_T2_suffix.eml",
    make_msg("运单草单_YXO-2026-661_CQWLJT260810001-SPB_ABCU1111111",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("ABCU1111111-260811-010000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T2", "action": "pending",
     "note": "序号260810001在库同时命中-SPB与-VXN(两active,同箱)→T2待办"})

add("draft", "D10_T3_box.eml",
    make_msg("运单草单_YXO-2026-662_CQWLJT260810002-SPB_ABCU9999999",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("ABCU9999999-260811-020000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T3", "action": "pending",
     "note": "客编260810002-SPB库箱号ABCU2222222，来信箱号ABCU9999999→T3待办"})

add("draft", "D11_T4_unknown.eml",
    make_msg("运单草单_YXO-2026-663_CQWLJT999999999-SPB_ZZZU0000001",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("ZZZU0000001-260811-030000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T4", "action": "alarm",
     "note": "序号999999999不在库→T4报警（不进待办）"})

add("draft", "D12_T5_box_only.eml",
    make_msg("装箱单_ABCU4444444",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="箱号ABCU4444444相关草单见附件。",
             attachments=[("ABCU4444444-260811-040000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T5", "action": "alarm",
     "note": "主题无可解析客编，但箱号ABCU4444444唯一命中→T5报警（不进待办）"})

add("draft", "D13_T6_box_reuse.eml",
    make_msg("装箱单_ABCU5555555",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="箱号ABCU5555555相关草单见附件。",
             attachments=[("ABCU5555555-260811-050000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T6", "action": "pending",
     "note": "箱号ABCU5555555命中2条active(不同客编)→T6待办人工挑"})

add("draft", "D14_T7_cross_suffix.eml",
    make_msg("运单草单_YXO-2026-664_CQWLJT260810005-SPB_ABCU6666666",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收草单。",
             attachments=[("ABCU6666666-260811-060000已加密.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T7", "action": "pending",
     "note": "序号260810005跨-SPB/-VXN两active后缀(同箱)→T7防御性guard待办"})

add("draft", "D15_T0_nothing.eml",
    make_msg("周末值班安排",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="本周值班表见正文，无客编无箱号。"),
    {"type": "draft", "category": "A", "tier": "T0", "action": "alarm",
     "note": "无可解析客编且无可解析箱号→T0报警"})

add("draft", "D16_draft_criterion2.eml",
    make_msg("运单草单_YXO-2026-665_CQWLJT260713004-BLLST_TSRU8008478",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="请查收更新草单，以本附件为准。",
             attachments=[("TSRU8008478.pdf", pdf_bytes())]),
    {"type": "draft", "category": "A", "tier": "T1", "action": "forward",
     "note": "判据2兜底：箱号.pdf + 正文含'更新草单'→仍识别为草单件"})

add("draft", "D17_multi_att.eml",
    make_msg("运单草单_YXO-2026-666_CQWLJT260713005-SPB_XXLU0000002",
             "刘婷", "liuting@yxologistics.com", MON_TO,
             body_text="草单及补充资料。",
             attachments=[("XXLU0000002-260811-070000已加密.pdf", pdf_bytes()),
                          ("补充说明.docx", b"PK test docx")]),
    {"type": "draft", "category": "A", "tier": "T1", "action": "forward",
     "note": "多附件，只要有一个命中加密PDF即判草单件"})

# =====================================================================
# 3) 运单号 WAYBILL（9 例，含多行/.xls 拆分专项）
# =====================================================================
def wb_att(rows, name="运单号明细.xlsx"):
    return [(name, waybill_xls(rows))]

# W01 单箱号单行-外部公司分组转发xls+正文
add("waybill", "W01_single_external.eml",
    make_msg("CQWLJT260816001-MA 运单号草单",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号已出，请见附件小xls。",
             attachments=wb_att([{"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"}])),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "外部公司(东盟/call56)按客编分组转发小xls+正文；单行即单封",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA"],"has_xls":True}],
               "skip_rows": [], "alarm_rows": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# W02 内部同事仅企微（无外部收件人）
add("waybill", "W02_internal_only.eml",
    make_msg("CQWLJT260816003-BLLST 运单号草单",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号已出。",
             attachments=wb_att([{"客户编码":"CQWLJT260816003-BLLST","箱号":"TEMU9000003","运单号":"YX2600003"}],
                                name="CQWLJT260816003-BLLST.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "内部子公司(布拉茨克)→外部无收件人，仅企微通知负责同事",
     "split": {"email_parts": 0, "wecom_only_parts": 1,
               "parts": [{"company":"布拉茨克子公司","route":"internal","rows":["CQWLJT260816003-BLLST"],"has_xls":False}],
               "skip_rows": [], "alarm_rows": []},
     "forward": {"emails": [], "wecom": ["布拉茨克子公司负责同事"]}})

# W03 多外部收件人（同封多客编，均外部）
add("waybill", "W03_multi_external.eml",
    make_msg("CQWLJT260816001-MA / CQWLJT260816002-MA 运单号草单",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="两票运单号草单。",
             attachments=wb_att([{"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"},
                                 {"客户编码":"CQWLJT260816002-MA","箱号":"TEMU9000002","运单号":"YX2600002"}])),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "一封含多客编(均东盟外部)→按外部公司拆1封(同公司合并)带小xls(2行)",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA","CQWLJT260816002-MA"],"has_xls":True}],
               "skip_rows": [], "alarm_rows": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# W04 无xls附件（容错）
add("waybill", "W04_no_xls.eml",
    make_msg("CQWLJT260816001-MA 运单号草单",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号已出（无附件，正文在下面）。"),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "无xls附件→按客编转发正文（无小xls），不应崩溃"})

# W05 待确认/报警路径（客编不在库）
add("waybill", "W05_unknown.eml",
    make_msg("CQWLJT999999998-MA 运单号草单",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号已出。",
             attachments=wb_att([{"客户编码":"CQWLJT999999998-MA","箱号":"TEMU9999998","运单号":"YX9999998"}],
                                name="CQWLJT999999998-MA.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T4",
     "action": "alarm_or_pending",
     "note": "客编不在库→运单号侧报警/待确认（依运单号机器人实现）"})

# ---- W06 多行拆分核心：一行外部+一行外部+一行内部 → 拆2份(1封外部邮件+1个内部企微) ----
add("waybill", "W06_multirow_mixed.eml",
    make_msg("运单号草单汇总 CQWLJT260816001/002/003",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="以下多票运单号草单，详见附件。",
             attachments=wb_att([
                 {"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"},
                 {"客户编码":"CQWLJT260816002-MA","箱号":"TEMU9000002","运单号":"YX2600002"},
                 {"客户编码":"CQWLJT260816003-BLLST","箱号":"TEMU9000003","运单号":"YX2600003"},
             ], name="运单号汇总.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "一封xls含3行：2行东盟(call56外部)+1行布拉茨克(内部)→拆成2份：1封外部邮件(小xls含2行)+1个内部企微通知(不收邮件)",
     "split": {"email_parts": 1, "wecom_only_parts": 1,
               "parts": [
                   {"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA","CQWLJT260816002-MA"],"has_xls":True},
                   {"company":"布拉茨克子公司","route":"internal","rows":["CQWLJT260816003-BLLST"],"has_xls":False},
               ],
               "skip_rows": [], "alarm_rows": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"],
                 "wecom": ["布拉茨克子公司负责同事"]}})

# ---- W07 多行同行外部公司：2行均东盟 → 拆1封带完整小xls(2行) ----
add("waybill", "W07_multirow_one_co.eml",
    make_msg("运单号草单 CQWLJT260816001/002",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="以下两票运单号草单。",
             attachments=wb_att([
                 {"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"},
                 {"客户编码":"CQWLJT260816002-MA","箱号":"TEMU9000002","运单号":"YX2600002"},
             ], name="运单号两票.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "一封xls含2行(均东盟外部)→按外部公司拆1封，小xls含全部2行",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA","CQWLJT260816002-MA"],"has_xls":True}],
               "skip_rows": [], "alarm_rows": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# ---- W08 多行含退舱行：退舱行跳过，其余正常拆 ----
add("waybill", "W08_multirow_with_cancel.eml",
    make_msg("运单号草单 CQWLJT260816001/260713006-VXN",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号草单（含一条退舱记录，应自动跳过）。",
             attachments=wb_att([
                 {"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"},
                 {"客户编码":"CQWLJT260713006-VXN","箱号":"TCLU3000001","运单号":"YX2600006"},  # 退舱(D07)
                 {"客户编码":"CQWLJT260816003-BLLST","箱号":"TEMU9000003","运单号":"YX2600003"},
             ], name="运单号含退舱.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T1",
     "action": "split_forward",
     "note": "一封xls含3行，第2行CQWLJT260713006-VXN状态=退舱→跳过该行；其余2行正常拆(东盟邮件+布拉茨克企微)",
     "split": {"email_parts": 1, "wecom_only_parts": 1,
               "parts": [
                   {"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA"],"has_xls":True},
                   {"company":"布拉茨克子公司","route":"internal","rows":["CQWLJT260816003-BLLST"],"has_xls":False},
               ],
               "skip_rows": ["CQWLJT260713006-VXN(退舱)"], "alarm_rows": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"],
                 "wecom": ["布拉茨克子公司负责同事"]}})

# ---- W09 多行含未命中行：未命中行报警/待确认，其余正常拆 ----
add("waybill", "W09_multirow_with_unknown.eml",
    make_msg("运单号草单 CQWLJT260816001/999999998-MA",
             "单证万邦", "docwbfb@yxologistics.com", MON_TO,
             body_text="运单号草单（含一条库内无此客编，应报警/待确认）。",
             attachments=wb_att([
                 {"客户编码":"CQWLJT260816001-MA","箱号":"TEMU9000001","运单号":"YX2600001"},
                 {"客户编码":"CQWLJT999999998-MA","箱号":"TEMU9999998","运单号":"YX9999998"},  # 不在库
                 {"客户编码":"CQWLJT260816003-BLLST","箱号":"TEMU9000003","运单号":"YX2600003"},
             ], name="运单号含未知.xlsx")),
    {"type": "waybill", "category": "WAY_A", "tier": "T4",
     "action": "split_forward",
     "note": "一封xls含3行，第2行CQWLJT999999998-MA不在库→该行报警/待确认；其余2行正常拆(东盟邮件+布拉茨克企微)",
     "split": {"email_parts": 1, "wecom_only_parts": 1,
               "parts": [
                   {"company":"东盟子公司","route":"external(call56)","rows":["CQWLJT260816001-MA"],"has_xls":True},
                   {"company":"布拉茨克子公司","route":"internal","rows":["CQWLJT260816003-BLLST"],"has_xls":False},
               ],
               "skip_rows": [], "alarm_rows": ["CQWLJT999999998-MA(库内无客编)"]},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"],
                 "wecom": ["布拉茨克子公司负责同事"]}})

# =====================================================================
# 4) 运踪 TRACING（3 例）
# =====================================================================
def tracing_body():
    return ("<html><body>Daily container list:<table>"
            "<tr><th>Container</th><th>Destination</th></tr>"
            "<tr><td>TEMU1234567</td><td>Moscow</td></tr>"
            "</table></body></html>")

add("tracing", "T01_standard.eml",
    make_msg("Tracing info of YXO 2026 train 611 - date 2026-08-26 [YXO-2026-]611-K",
             "Tracing System", "tracing-system@yxologistics.com", MON_TO,
             body_html=tracing_body(),
             attachments=[("611_Daily_container_list.xls", tracing_xls_bytes())]),
    {"type": "tracing", "category": "-", "tier": "-", "action": "fanout_forward",
     "note": "按train 611扇出转发给该班列对应公司；整封(含xls)透传，不解析xls"})

add("tracing", "T02_no_box.eml",
    make_msg("Tracing info of YXO 2026 train 612 - date 2026-08-26 [YXO-2026-]612-K",
             "Tracing System", "tracing-system@yxologistics.com", MON_TO,
             body_text="班列612已发车，暂无箱号明细。"),
    {"type": "tracing", "category": "-", "tier": "-", "action": "fanout_forward",
     "note": "正文无箱号→box_no为空，仍照常扇出转发（容错）"})

add("tracing", "T03_segment.eml",
    make_msg("Tracing info of YXO 2026 train 613 - date 2026-08-26 [YXO-2026-]613-K-1",
             "Tracing System", "tracing-system@yxologistics.com", MON_TO,
             body_html=tracing_body()),
    {"type": "tracing", "category": "-", "tier": "-", "action": "fanout_forward",
     "note": "主题含-1分段标记；机器人不解析K段，每封独立扇出，-1/-2/-3互不合并"})

# =====================================================================
# 5) DSK（3 例）
# =====================================================================
def dsk_html(box, company):
    return (f"<html><body><table><tr><th>Container</th><th>Company</th></tr>"
            f"<tr><td>{box}</td><td>{company}</td></tr></table>"
            f"<p>DSK info for {box}</p></body></html>")

add("dsk", "K01_box_html.eml",
    make_msg("DSK TGLU7755223",
             "KASA", "kasa@rtsb.de", MON_TO,
             body_html=dsk_html("TGLU7755223", "东盟子公司")),
    {"type": "dsk", "category": "-", "tier": "-", "action": "split_forward",
     "note": "发件kasa@rtsb.de；箱号TGLU7755223 in HTML表格→按箱号拆分转发"})

add("dsk", "K02_no_box.eml",
    make_msg("DSK 通知",
             "KASA", "kasa@rtsb.de", MON_TO,
             body_text="DSK通知，但邮件未含箱号。"),
    {"type": "dsk", "category": "-", "tier": "-", "action": "record_only",
     "note": "无箱号→不拆分，记录/跳过（依DSK机器人实现）"})

add("dsk", "K03_multi_box.eml",
    make_msg("DSK 多箱",
             "KASA", "kasa@rtsb.de", MON_TO,
             body_html=("<html><body><table><tr><th>Container</th></tr>"
                        "<tr><td>TGLU7755223</td></tr><tr><td>TEMU1234567</td></tr>"
                        "</table></body></html>")),
    {"type": "dsk", "category": "-", "tier": "-", "action": "split_forward",
     "note": "HTML含多箱号→逐箱拆分转发"})

# =====================================================================
# 6) ATB（6 例，含逐箱号拆分专项）
# =====================================================================
def atb_html(boxes):
    rows = "".join(f"<tr><td>{b}</td></tr>" for b in boxes)
    return (f"<html><body><table><tr><th>Container</th></tr>{rows}"
            f"<tr><td>ATB info</td></tr></table></body></html>")

# A01 单箱号 in HTML → 原样整封转发
add("atb", "A01_box_html.eml",
    make_msg("ATB CQWLJT260816001-MA",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_html=atb_html(["TEMU9000001"])),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "发件atb@yxologistics.com；箱号TEMU9000001 in HTML→原内容整封转发",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"box":"TEMU9000001","company":"东盟子公司","route":"external(call56)","kind":"full_original"}],
               "skip_boxes": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# A02 HTML无box，主题回退
add("atb", "A02_subject_fallback.eml",
    make_msg("ATB container TEMU9000002",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_text="ATB通知（HTML无箱号，箱号在主题）。"),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "HTML无箱号→回退到主题提取箱号TEMU9000002→转发",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"box":"TEMU9000002","company":"东盟子公司","route":"external(call56)","kind":"full_original"}],
               "skip_boxes": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# A03 多箱号 in HTML → 逐箱号各发一封（原样）
add("atb", "A03_multi_box.eml",
    make_msg("ATB 多箱 CQWLJT260816001-MA",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_html=atb_html(["TEMU9000001", "TEMU9000003"])),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "HTML含2箱号→逐箱号各发一封整封原样转发(每封To按该箱号路由)",
     "split": {"email_parts": 2, "wecom_only_parts": 0,
               "parts": [
                   {"box":"TEMU9000001","company":"东盟子公司","route":"external(call56)","kind":"full_original"},
                   {"box":"TEMU9000003","company":"布拉茨克子公司","route":"internal","kind":"full_original"},
               ],
               "skip_boxes": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)", "布拉茨克子公司负责同事(内部原样)"],
                 "wecom": []}})

# A04 多箱号跨3个公司 → 拆3封
add("atb", "A04_multi_box_3co.eml",
    make_msg("ATB 多箱跨公司",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_html=atb_html(["TEMU9000001", "TEMU9000003", "TEMU9000004"])),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "HTML含3箱号(东盟/布拉茨克/明斯克)→逐箱号各发一封整封原样转发",
     "split": {"email_parts": 3, "wecom_only_parts": 0,
               "parts": [
                   {"box":"TEMU9000001","company":"东盟子公司","route":"external(call56)","kind":"full_original"},
                   {"box":"TEMU9000003","company":"布拉茨克子公司","route":"internal","kind":"full_original"},
                   {"box":"TEMU9000004","company":"明斯克子公司","route":"external","kind":"full_original"},
               ],
               "skip_boxes": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)", "布拉茨克子公司负责同事(内部原样)", "明斯克子公司外部收件人"],
                 "wecom": []}})

# A05 多箱号含1个无路由 → 无路由箱号跳过，其余照发
add("atb", "A05_multi_box_unrouted.eml",
    make_msg("ATB 含无效箱号",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_html=atb_html(["TEMU9000001", "ZZLU0000001"])),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "HTML含2箱号，ZZLU0000001不在箱号→公司映射(无路由)→该行跳过；TEMU9000001照发",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [
                   {"box":"TEMU9000001","company":"东盟子公司","route":"external(call56)","kind":"full_original"},
               ],
               "skip_boxes": ["ZZLU0000001(无路由配置)"]},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# A06 箱号在HTML与主题重复 → 去重只发1封
add("atb", "A06_dup_box.eml",
    make_msg("ATB container TEMU9000001 重复",
             "ATB", "atb@yxologistics.com", MON_TO,
             body_html=atb_html(["TEMU9000001"])),
    {"type": "atb", "category": "-", "tier": "-", "action": "forward",
     "note": "主题与HTML均含TEMU9000001→extract_boxes去重只1个箱号→仅发1封",
     "split": {"email_parts": 1, "wecom_only_parts": 0,
               "parts": [{"box":"TEMU9000001","company":"东盟子公司","route":"external(call56)","kind":"full_original"}],
               "skip_boxes": []},
     "forward": {"emails": ["东盟子公司外部收件人(call56)"], "wecom": []}})

# =====================================================================
# 写出
# =====================================================================
os.makedirs(OUT, exist_ok=True)
# records.csv
with open(os.path.join(OUT, "records.csv"), "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=["客户编码", "箱号", "开票子公司名称", "状态", "is_deleted"])
    w.writeheader()
    for r in records:
        w.writerow(r)
# draft_nums_seed.txt (B类台账种子：数字段)
with open(os.path.join(OUT, "draft_nums_seed.txt"), "w", encoding="utf-8") as f:
    f.write("260709001\n")  # D03 数字段，使该客编被识别为 B
# eml
for sub, fn, msg in cases:
    d = os.path.join(OUT, sub)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, fn), "wb") as f:
        f.write(msg.as_bytes())
# manifest
with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=2)

print(f"生成完成: {len(cases)} 个夹具")
print(f"  draft   : {sum(1 for m in manifest if m['type']=='draft')}")
print(f"  waybill : {sum(1 for m in manifest if m['type']=='waybill')}")
print(f"  tracing : {sum(1 for m in manifest if m['type']=='tracing')}")
print(f"  dsk     : {sum(1 for m in manifest if m['type']=='dsk')}")
print(f"  atb     : {sum(1 for m in manifest if m['type']=='atb')}")
print(f"  records : {len(records)} 行")
