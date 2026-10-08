# wecombot —— 企微机器人（:5001）

> ⚠️ **改动本目录之前，先读 §2「版本漂移」——仓库里的这份，和生产跑的那份不是同一份。**

---

## 1. 它是什么

一个**独立的 Flask 服务**（waitress，监听 `0.0.0.0:5001`），**只被企业微信服务器调用，没有人类界面**。

对外只有三个路由：`/`（状态页）、`/wecom/callback`（GET+POST）、`/health`。

两条通道：
- **自建应用**：被动 XML 回复（`server.py`）
- **微信客服（kf）**：主动拉取外部用户消息 + 回复（`cs_bot/wecom_api.py` 的 `kf_sync_msgs` / `kf_send_text`）

> 微信客服是**唯一**能让「普通微信用户」（外部的人，不用装企业微信）直接给公司发消息的官方通道。
> ⚠️ **限制：48 小时内不给客服发消息，这条通路就关闭**（毛骁洋 2026-10-06 确认）。

---

## 2. ⚠️ 版本漂移（最重要的注意事项）

| | 位置 | 状态 |
|---|---|---|
| **生产真源** | 服务器 `D:\YXO_DATA\WeComBot` | **独立、不随 git 更新** |
| 仓库副本 | 本目录 | **已过期** |

**实证（2026-10-04 生产探测）**：生产 `:5001` 的 `/api/notify` 返回 **405**（存在，只收 POST），而**仓库 `wecombot/server.py` 里根本没有 `@app.route("/api/notify")`**。

⇒ **任何改动本目录之前**：先确认生产版长什么样、差异在哪；改完还要考虑怎么回流。**不要假设"仓库 = 生产"。**

---

## 3. 分层与破口

```
server.py        HTTP / 企微协议 / 路由
  └─ cs_bot/engine.py      会话与业务（纯文本进出）
       └─ store.py / files.py / mailer.py      数据与 IO
       └─ wecom_api.py                        企微 API 封装
```

**这一刀是干净的**：`engine.py` **不 import** `wecom_api`。

**已知破口**（改之前先知道）：
- **硬编码外部目录**：`engine.py` 与 `wecom_api.py` 里有 `sys.path.insert(0, r"D:\YXO_DATA\MailBots")` —— 业务模块靠另一个程序目录才能 import。
- **魔法字符串穿透层**：engine 返回 `"__BIND__"+name`，由 HTTP 层解析并写库。
- **数据层被绕过**：`wecom_api.py` 自己拼 `../data/cs_bot.db` 读绑定，与 `store.py` 用的路径是**两处定义**。
- **文案 / emoji 写死在 engine 里**。
- **飞书死逻辑还在**：双写已弃用，但 `fs_ok` 恒 True、回执仍打印"飞书 ✅"。
- **宽泛 `except`**：`server.py` 9 处、`engine.py` 13 处、`wecom_api.py` 7 处，含多处 `except Exception: pass`。

---

## 4. 数据

| 库 | 位置 | 用途 |
|---|---|---|
| `cs_bot.db` | 生产 `D:\YXO_DATA\WeComBot\data\` | 撤销历史 / 绑定 |
| `yxo.db` | 主站生产路径 | **只读**（读业务数据） |
| JSON 状态 | 同目录 | sessions / `kf_cursor.json` / blacklist |

---

## 5. 已知重复与缺口

- ⚠️ **`requirements.txt` 装不起来**：只有 flask + pycryptodome，**缺 `requests`**（`wecom_api.py` 用）和 **`waitress`**（`server.py` 用）。
- ⚠️ **重复定义**：`WECOM_USER_MAP` 两份（本目录 `config.py` vs 根 `config.py`）；`USER_COMPANIES` 两份；同事名单**第三份**硬编码在 `server.py` 里。
- ⚠️ **无任何测试**（仓库 `tests/` 里没有 wecombot / cs_bot 的用例）。
- 配置缺失时的行为：`wecom_api` 会装"空壳"模块，飞书类调用报错但**不崩、非静默**。

---

## 6. 采纳现状（重要，影响一切取舍）

**企微机器人除作者本人外基本无人使用**——查信息、落库写入都没人用。原因是额外学习成本、同事宁愿保持现有操作节奏。

⇒ **铁律：不要把任何流程的必要环节依赖在企微上。** 对外部合作方（不在你的企微组织里）**根本够不到**——企微通知只认内部 4 个人的 UserID。

⇒ 需要触达外部时，**走邮件**（你们本来就在给渝新欧发草单/运单号/ATB）。

---

## 7. 怎么跑（本机）

```powershell
wecombot\start_server.bat     # pythonw 拉起 server.py，监听 :5001
```

生产上它同样是独立进程，与主站互不干扰。
