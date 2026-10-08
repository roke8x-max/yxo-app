# wecombot —— 企微机器人

独立 Flask 服务（waitress 监听 `0.0.0.0:5001`），只被企微服务器调用。

## 当前生效（Current）

暂无。**模块说明见代码目录的 [`../../wecombot/README.md`](../../wecombot/README.md)。**

## 讨论中（Drafts）

暂无。

## 🔴 三条必须知道的事实

> **① 仓库副本落后于生产（版本漂移）**
> 生产真源是服务器 `D:\YXO_DATA\WeComBot`，**独立、不随 git 更新**，仓库副本已过期。
> 实证：生产 `:5001` 有 `/api/notify`（主站/机器人调它发通知），而**仓库 `wecombot/server.py` 里根本没有该路由**（只有 `/`、`/wecom/callback`、`/health`）。
> ⇒ **改企微代码前先确认改的是生产那份还是仓库那份**；把仓库直接部署上去会打断通知链路。

> **② 企微 8765 回调断（已知限制）**
> 生产 WeComBot 无 8765 出站路由 ⇒ inbound「确认 N」在生产不通。
> （旧 AGENTS.md「已知坑」救回——新体系里原本找不到落点。）

> **③ 不要把任何流程的必要环节依赖在企微上**
> 企微机器人**除作者本人外基本无人使用**；触达外部合作方**只能靠邮件**。

## 已归档（Archive）

暂无。

## 相关代码

`wecombot/server.py`（路由）｜`wecombot/wecom_api.py`（发送与姓名解析）｜生产 `D:\YXO_DATA\WeComBot`（**真源**）

> 📌 通知链路的现状（2026-09-20 起的「方案 B」）：`mailbots_next/core/notify.py` **不再 import 旧 `wecombot` 包**，改为 `from .wecom_notify import WeComHttpClient`，通过 HTTP 调 WeComBot 的 `/api/notify`（带 `X-Notify-Token`）。旧链路已不存在。
