# spec：C 组 —— 企微通知改走 HTTP 调 WeComBot（`mailbots_next` 侧薄客户端）

> 交付对象：OpenCode ｜ 验收：芙蕾雅 ｜ 决策人：洋（2026-09-18 定方向 / 2026-09-20 补齐事实）
> **一句话任务**：把 `mailbots_next` 发企微通知的方式，从「**import 旧 `wecombot` 包**」改成「**HTTP 调用同机运行的 WeComBot 服务**」（`POST http://127.0.0.1:5001/api/notify`）。
> 端点**已就绪并经验证**（见 §6），可以直接开工。

---

## 1. 为什么这么做（背景，供理解，不需要你查历史）

- 现状：`mailbots_next/core/notify.py` 里 `from wecombot.cs_bot.wecom_api import notify_by_name` —— 依赖仓库里的旧包；而**生产实际跑的是另一份代码**（`D:\YXO_DATA\WeComBot`，不随 git 更新）。旧包用 `sys.path` 魔法把 `from config import` 解析到它自己的 `config.py`，**脆弱且已经踩过一次坑**。
- 现在 WeComBot 已提供 `/api/notify` 端点，所以 `mailbots_next` 侧只需一个**薄 HTTP 客户端**。
- **原则**：企微那套逻辑（access_token 缓存 / 双通道 / 微信客服 / 1800 字节分段）**全部留在 WeComBot 一处**，我们这边一行都不复制 —— 否则那 300 行会在两份代码里各养一份。

---

## 2. 现状事实（已核实，直接按此实现）

| 位置 | 现状 |
|---|---|
| `mailbots_next/core/notify.py:72-82` | `_init_client()`：`from wecombot.cs_bot.wecom_api import notify_by_name`；失败则 `self._notify_by_name = None` + **启动期记一次 ERROR**（模块级 flag `_client_unavailable_logged`） |
| `mailbots_next/core/notify.py:87-93` | `_WECOM_NAME_BY_EMAIL`：**邮箱 → 企微真名** 映射（4 位同事 + `ops@example.com`）。这是**仓库内唯一允许出现真名的位置** |
| `mailbots_next/core/notify.py:95-135` | `notify(notify_type, recipients, content, error_id) -> bool`。入参 `recipients` 是**邮箱列表**；循环里把邮箱转真名后调 `self._notify_by_name(name, content)`，**期望返回 `(ok, channel)` 二元组**；未映射的走 `_fallback_owner_email()` 降级 |
| `mailbots_next/core/notify.py:103-107` | `_notify_by_name` 为 `None` ⇒ ERROR + `increment_counter("notify_failed")` + 返回 `False`（**B 组 T3a 的 loud fail 语义，必须保留**） |
| `mailbots_next/core/notify.py:137-170` | `_fallback_owner_email()`：SMTP 降级（未映射收件人）—— **本任务不要动它** |
| `mailbots_next/config/secrets.py` | `get_secrets()` 读取 gitignored 的 `mailbots_next/secrets.json`，**有进程内缓存** ⇒ 改配置需要重启 |

**关键约束**：`notify()` 传给客户端的只有**真名**，而白名单配置的是**邮箱** ⇒ 所以 `try_kf` 的判定要有办法拿到邮箱（见 §3.3）。

---

## 3. 要做的改动

### 3.1 新增 `mailbots_next/core/wecom_notify.py`（薄客户端，目标 ~60 行）

```python
class WeComConfigMissing(Exception):
    """URL 或 token 未配置。"""

class WeComHttpClient:
    @classmethod
    def from_secrets(cls) -> "WeComHttpClient": ...
        # 读 WECOM_NOTIFY_URL / WECOM_NOTIFY_TOKEN / WECOM_KF_EMAILS
        # 环境变量优先于 secrets.json；URL 或 TOKEN 缺失 ⇒ 抛 WeComConfigMissing
    def kf_enabled(self, email: str) -> bool: ...
        # email 在白名单内（大小写不敏感、strip 后比较）⇒ True
    def notify_by_name(self, name: str, text: str, try_kf: bool = False) -> tuple:
        # 返回 (ok: bool, channel: str) —— 与旧 wecom_api.notify_by_name 的返回形态保持一致
```

**逐条要求：**

1. **调用**：`POST {WECOM_NOTIFY_URL}`，请求头 `X-Notify-Token: <token>`、`Content-Type: application/json`，body
   ```json
   {"name": "<同事真名>", "text": "<通知正文>", "try_kf": false}
   ```
2. **配置来源**（环境变量优先，其次 `mailbots_next/secrets.json`）：
   `WECOM_NOTIFY_URL`（生产 `http://127.0.0.1:5001/api/notify`）、`WECOM_NOTIFY_TOKEN`、`WECOM_KF_EMAILS`（逗号分隔的邮箱白名单，**当前为空 = 谁都不走微信客服**）。
3. **超时**：connect 3s / **read 45s**；**不重试**（只发 1 次请求）。
   ⚠️ **read 必须 ≥45s** —— 2026-09-20 生产实测：`try_kf=true` 时 WeComBot 侧要先走微信客服、**耗时 35.8 秒**才失败回退到应用消息，然后才返回 `200`。若设成 5s，**白名单内同事的每条通知都会"我们报超时失败、对方其实收到了"**。
   白名单外的人走 `try_kf=false` ⇒ WeComBot 直接发应用消息、秒回，45s 只是**上限**、不是实际等待。
4. **成功判据**：HTTP **200 且** body `{"ok": true}`。其余（超时 / 连接失败 / 401 / 非 200 / `ok=false`）
   ⇒ 返回 `(False, channel_or_empty)`，并 `_log.error` + `increment_counter("notify_failed")`；**不抛异常**。
   失败日志只记**状态码 / 异常类型 / channel**，**不记 token、不记正文全文**。
5. **`channel` 取值**：把端点返回的 `channel` 原样透出（`"kf"` / `"app"` / `""`），便于日志区分走了哪条通道；拿不到就用 `""`。
6. **不得**写任何企微 token / 客服 / 分段逻辑（见 §4）。

### 3.2 `notify.py`：替换客户端构造（**`notify()` 主体的 loud fail 语义不动**）

`_init_client()` 改为：

```python
    def _init_client(self):
        try:
            from .wecom_notify import WeComHttpClient
            self._client = WeComHttpClient.from_secrets()
            self._notify_by_name = self._client.notify_by_name
        except Exception as e:
            self._client = None
            self._notify_by_name = None
            global _client_unavailable_logged
            if not _client_unavailable_logged:
                _client_unavailable_logged = True
                _log.error(f"WeCom client not available ({type(e).__name__}), "
                           f"notifications will loud-fail with notify_failed counter")
```

- **删掉** `from wecombot...`（本任务的核心目的）。
- `_notify_by_name` 仍保持「可用时是一个 `(name, text) -> (ok, channel)` 的可调用对象、不可用时为 `None`」⇒ 现有 loud fail 路径与 `notify()` 的主体逻辑**不需要重写**。
- `get_notifier()` 的对外接口**不得变化**（`notify()` / `send_alarm()` / `send_pending()` / `send_program_error()` 的签名与返回语义都不变）。

### 3.3 `try_kf` 怎么定（**这是本组唯一需要你想清楚的点**）

`notify()` 循环里拿到的是**邮箱**（`recipient`），而客户端 `notify_by_name` 收到的是**真名**。所以：

```python
    # notify() 循环内，把原调用改成：
    ok, channel = self._notify_by_name(name, content, self._client.kf_enabled(recipient))
```

即：**由 `notify.py` 用邮箱向客户端要白名单判定**，然后把结果作为参数传下去。这样：
- 白名单配置仍按**邮箱**（与 `WECOM_NOTIFY_URL` / `WECOM_NOTIFY_TOKEN` 同一处，便于管理）；
- 薄客户端**不需要**知道 `notify.py` 的真名表，也不需要反向查表。

> ⚠️ `self._client` 只有在 `_notify_by_name` 非 `None` 时才被使用，二者同时为 `None`，所以不必额外防御 —— 但你要保证这一点成立。

---

## 4. 不得做的事（红线：别把 ① 做成 ③）

- ❌ **不要在 `mailbots_next` 里实现**企微 `access_token`、双通道、微信客服（kf）、`external_userid`、1800 字节分段 —— 那些是 WeComBot 的职责。
- ❌ **不需要** `corp_id` / `agent_id` / `secret` / `open_kfid` / `KF_SECRET` / 客服绑定表 —— 这些**都不进 `mailbots_next`**。
- ❌ **不 import `wecombot`、也不 import WeComBot 的任何模块**（只走 HTTP）—— 否则又回到 `sys.path` 地雷。
- ❌ 不改 `_fallback_owner_email()`（SMTP 降级路径）。
- ❌ 不把 `_WECOM_NAME_BY_EMAIL` 的真名扩散到新文件（**它是仓库内唯一允许出现真名的位置**，新客户端只接收"名字"这一个字符串，不自己维护映射）。
- ❌ 不加新依赖（用标准库 `urllib.request` 或已有的 `requests`，看你所在分支已有什么；**优先标准库**，避免动 `requirements`）。
- ❌ **不要 `git commit`、不要 `git push`**（改完停在工作区）。

---

## 5. 测试要求（新建 `mailbots_next/tests/test_wecom_notify.py`）

**全部用假 HTTP 桩，禁止打真企微。**

| # | 场景 | 期望 |
|---|---|---|
| 1 | 桩返回 `200 {"ok":true,"channel":"app"}` | 返回 `(True, "app")`；并断言请求 body 里 `name` 是**真名**、`try_kf` 按白名单取值、请求头带 `X-Notify-Token` |
| 2 | 桩返回 `401` / `500` / `200 {"ok":false}` | 返回 `(False, ...)` + `notify_failed` 计数 +1 + 一条 ERROR |
| 3 | 桩连接失败 / 超时 | 同上；**且断言只发了 1 次请求**（不重试） |
| 4 | `WECOM_NOTIFY_URL` 或 `TOKEN` 未配 | `notify.py` 侧表现为 `_notify_by_name is None`（构造即失败）；**断言没有发出任何请求** |
| 5 | 邮箱在白名单内 / 不在 | `try_kf` 分别为 `true` / `false`（含大小写与前后空格） |
| 6 | 收件人邮箱不在 `_WECOM_NAME_BY_EMAIL` | 仍走现有降级路径（ERROR + 计数 + SMTP 降级），**不静默** |
| 7 | 失败日志 | 不含 token、不含正文全文（对日志文本断言） |
| 8 | 超时值 | 断言客户端配置的 read timeout **≥ 40 秒**（防有人"顺手改回 5s"） |

### 🔴 真锁要求（硬要求）

改完后自己把下面两条各撤一次，跑一遍，**并贴原始输出**：

- 把 `try_kf` 恒置 `true` ⇒ **用例 5 必挂**；
- 把 read timeout 改回 5s ⇒ **用例 8 必挂**；
- 删掉失败路径的 `increment_counter("notify_failed")` ⇒ **用例 2 或 3 必挂**。

每次撤改后**还原**，并核对文件 `md5` 与改动前一致。

### 既有测试的适配（**必须处理，别漏**）

`mailbots_next/tests/test_notify_email.py` 的 `TestWeComLoudFail` 目前用 `monkeypatch.setitem(sys.modules, "wecombot...", None)` 来制造"客户端不可用"。
**改造后不再 import `wecombot`，这个 patch 会失效** ⇒ 请把那几处改成按**新机制**制造失败（例如：让 `WECOM_NOTIFY_URL` / `TOKEN` 缺失，或桩返回 500），并保证：
- 原有的 loud fail 断言（**不抛、返回 `False`、`notify_failed` 计数 +1、启动期只记一次 ERROR**）**全部继续成立**；
- `test_notify_email.py` 里依赖 `wecombot` 的引用**清零**。

---

## 6. ✅ 端点已就绪（2026-09-20，可直接联调）

生产 `D:\YXO_DATA\WeComBot\` 已加好 `/api/notify` 并重启。验证分两部分：

**A. 芙蕾雅独立只读验证（我跑的）**

| 验证 | 结果 |
|---|---|
| `GET /health` | `200` |
| `POST /api/notify` 不带 token | `401 {"msg":"unauthorized","ok":false}`（鉴权在业务之前，不会触发消息发送） |
| `POST /api/notify` 错误 token | `401` |
| 生产 `server.py:295-301` 实现 | 与本方回执所附代码**逐字一致**（含 `hmac.compare_digest`） |
| `import hmac` / `NOTIFY_API_TOKEN` | 已就位（`server.py:10`、`config.py:38`、`server.py:33` import 列表） |

**B. 运维侧实测（小叽在服务器上执行 —— 数据来自她的回执，非我实测）**

| 验证 | 结果 |
|---|---|
| 空 text / 未知姓名 | `400 bad request` / `400 unknown name` |
| `try_kf=false` | `200 {"ok":true,"channel":"app"}`（**秒回**） |
| 默认（双通道） | `200 {"ok":true,"channel":"app"}`，**耗时 35.8 秒** ← §3.1 第 3 条把 read 定成 45s 的直接依据 |
| 端到端 | 已向运维负责人实发过消息并确认收到 |

> 联调时如需在服务器上真打一次，请**先问**再发（会对同事产生真实消息）。

---

## 7. 验收判据（芙蕾雅会逐条自己跑）

1. `grep -rn "wecombot" mailbots_next/ --include=*.py`（**排除 tests**）= **0 命中**；`tests/` 内引用也清零；
2. 新增用例 + 既有测试适配后，全量 `& $PY -m pytest -q -p no:cacheprovider` **0 failed**（以你开工时的采集数为基线，报告里写"基线 N → 现在 N+新增"，**不要写死绝对值**）；
3. 真锁往返的原始输出（三条，见 §5）；
4. `try_kf` 确实随白名单变化（用例 5），read timeout ≥40s（用例 8）；
5. 失败路径**不抛异常、不重试**、且 `notify_failed` 计数准确。

---

## 8. 交付要求

- 改动文件清单 + 每个文件改了什么（一行一句）；
- 新增/适配的用例数 + 全量结果（**基线 → 现在**）；
- 真锁往返的原始输出（撤改 → 哪条挂 → 还原 → md5 一致）；
- 与 spec 的**任何偏差都要单独列出**（不要自己拍板，也不要含糊过去）；
- 不要写 `git commit` / `git push` 的记录（本任务不提交）；
- 报告里**不要**写"防止漏信"这类因果不成立的解释；需要额外信息就直接问，**不要自行假设**。

---

*本 spec 已自包含：需要的背景、行号、端点证据都在上面。*
