# 运单号转发机器人启动广播：给 4 人发企微/微信通知
import sys, os
sys.path.insert(0, r"D:\YXO_DATA\WeComBot\cs_bot")
try:
    from wecom_api import notify_by_name
except Exception as e:
    print("IMPORT_FAIL", e)
    sys.exit(2)

people = ["毛骁洋", "杨雅雯", "冯茜", "韩文豪"]
text = (
    "【运单号转发机器人】已正式启动（LIVE 正式运行）\n"
    "开始监控 4 人邮箱「运单号」文件夹，推送 运单号下发 / 单证审核驳回 通知。\n"
    "forward_since 已设为启动时刻，仅处理新邮件，历史不误转。"
)
for p in people:
    try:
        ok, ch = notify_by_name(p, text)
        print(f"{p}: ok={ok} channel={ch}")
    except Exception as e:
        print(f"{p}: ERR {e}")
