# RBAC 补强 · B 工单：/api/stamp 加固（独立落码）

- **日期**：2026-09-29
- **状态**：**正式发出（自包含，给 OpenCode 落码；验收：芙蕾雅）**
- **性质**：本工单是 `2026-09-21-permission-rbac-module-design.md` **v2.5** + `2026-09-29-rbac-hardening-taskbook.md`（A+B 总任务书）中 **B 段**的**独立拆单**。B 不依赖 A（AUTH_ENABLED 灰度开关），也不依赖服务器 nginx 核查结果——核查只影响「部署时是否顺手摘 nginx location」这一**部署决策**，不阻塞落码。**B 现在就落、就测，等小叽服务器三查回传后再定部署顺序。**
- **落码：OpenCode；验收：芙蕾雅。**

---

## 0. 代码现状（已核实，无需再查）

- `app.py:861`：`if request.headers.get("X-Stamp-Token") != STAMP_TOKEN:` —— 用 `!=` 比较（时序侧信道）；无 IP 限制。
- `config.py:21`：弱默认 `yxo_stamp_local_2026`，缺 env 即生效。
- `rbac.py` 已登记 `stamp → public + X-Stamp-Token`（注释写明）。
- 调用方：旧 `mailbots/Dsk_Robot.py:730` / `Atb_Robot.py` 写死 `http://127.0.0.1:5011/api/stamp`，是**同机 localhost 进程**（非企微、非公网）。`mailbots_next` 已绕过此接口直写 yxo.db。
- 因此加固方向是**白名单 `127.0.0.1` + 拒代理头 + compare_digest + 去弱默认**，保持 `public + X-Stamp-Token` 形态（不改成 authenticated，因为旧机器人在跑）。

---

## 1. 改动（4 项）

### 1.1 拒代理头 + IP 白名单（纵深防御同机 nginx 反代绕过白名单）
函数体最前先拒代理头、再白名单：
```python
if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
    abort(403)
if request.remote_addr not in ("127.0.0.1", "::1"):
    abort(403)
```
> 本机 DSK/ATB 机器人直连 `http://127.0.0.1:5011/api/stamp`（`mailbots/Dsk_Robot.py:730`）不带代理头、`remote_addr=127.0.0.1`，**不误伤**；经 nginx 反代进来的外部请求带 `X-Forwarded-For`/`X-Real-IP` 会被直接拒（白名单命中后再验 token）。

### 1.2 compare_digest（防时序侧信道）
```python
import hmac
if not hmac.compare_digest(request.headers.get("X-Stamp-Token", "") or "", STAMP_TOKEN):
    abort(403)
```

### 1.3 去弱默认 + 缺失拒启（测试环境不崩）
- `config.py`：删除 `yxo_stamp_local_2026` 默认值；改为 `STAMP_TOKEN = os.environ.get("YXSTAMP_TOKEN")`（**导入期不 raise**，因为 `app.debug`/`app.testing` 此时尚未赋值）。
- **缺失判定移到 `init_auth(app)`**（此处才有 `app` 对象），调用**共享**判定 `is_production(app)`（定义见 §3 本工单内联）：
  ```python
  if is_production(app) and STAMP_TOKEN is None:
      raise RuntimeError("YXSTAMP_TOKEN 未设置，拒绝启动 /api/stamp")
  ```
- **`/api/stamp` 函数体内比较前先判 `None`**（防测试环境 `STAMP_TOKEN=None` 时 `compare_digest(x, None)` 抛 `TypeError`）：
  ```python
  if not STAMP_TOKEN or not hmac.compare_digest(request.headers.get("X-Stamp-Token", "") or "", STAMP_TOKEN):
      abort(403)
  ```
  `not STAMP_TOKEN` 短路后不再进入 `compare_digest`；测试 fixture 可 `monkeypatch`/设 env 注入 fake token。

### 1.4 rbac.py 注释
补「白名单 `127.0.0.1` + compare_digest，弱默认已删」。

---

## 2. 共享辅助 `is_production(app)`（内联，B 改动 1.3 引用，保证本工单自包含）
新增 `auth/common.py`（OpenCode 新建，A 工单也会复用同一文件，注意若 A 先合则直接 import）：
```python
import os
def is_production(app):
    # 唯一判定口径；bool() 包裹避免 PYTEST_CURRENT_TEST 返回字符串污染 or 表达式
    return not (
        bool(getattr(app, "debug", False)) or
        bool(getattr(app, "testing", False)) or
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )
```
> 注意：A 工单（AUTH_ENABLED 总任务书 §4.2）也定义同一 `is_production`。两工单合并部署时该文件只应存在一份；若先落 B，A 落码时直接 `from auth.common import is_production` 复用，不得重复定义。

---

## 3. B 自测项（OpenCode 落码后须自测并附证据）
- 本机 localhost 调用 stamp 成功：`curl -H "X-Stamp-Token: $YXSTAMP_TOKEN" http://127.0.0.1:5011/api/stamp` 返回正常。
- 从**非** `127.0.0.1` 调用返 403（白名单生效）。
- 带 `X-Forwarded-For` 头调用返 403（拒代理头生效）。
- 错 token 用 `compare_digest`（不可靠 timing 区分）。
- 缺 `YXSTAMP_TOKEN` 且 `is_production(app)` 为真 → 启动即 `raise`，**绝不带弱默认上线**。
- 测试环境（`PYTEST_CURRENT_TEST` 或 `app.testing`）缺 token 不崩（函数体 `not STAMP_TOKEN` 短路）。
- 补测试写入 `tests/auth_gate_test.py`（命名避开 gitignore 对 `test_*.py` 误伤）。

---

## 4. 部署后核查：B 部署后看 nginx access log（ds 第 1 点，修正顺序矛盾）
**B 部署后（不提前摘 nginx）立即做一次**——B 已加白名单+拒代理头，若 nginx 仍暴露 `/api/stamp`，外部 stamp 请求会被直接 403，**立刻能在 nginx access log 看到 403 响应**：
```powershell
# 在跑 nginx 的机器上
Select-String "api/stamp" <nginx_access_log_path> | Select-String " 403 "
# 或 grep:
grep "api/stamp" access.log | grep 403
```
> ⚠️ 403 是 4xx 响应、记在 **access.log** 而非 error.log，ds 已指正。
> ⚠️ **不要把「摘 nginx location」写成 B 的前置条件**：若先摘再部署 B，就看不到上述 403 观察窗口了。正确顺序：**先部署 B → 看 nginx access log 是否出现外部 stamp 403 → 再摘掉 nginx 的 `/api/stamp` location**（B 已防住，摘掉只是纵深清理）。
> 小叽的服务器三查（旧 DSK/ATB 机器人是否在跑 / nginx 是否暴露 `/api/stamp` / 调用 IP 是否全 127.0.0.1）只负责**记录**当前状态，**不要求提前摘除**；摘除动作放到 B 部署并确认观察日志之后。

---

## 5. 部署决策说明（不阻塞落码）
- B 的代码改动**完全不依赖**小叽的服务器三查结果。
- 三查结果只决定「部署 B 时是否同时摘掉 nginx 的 `/api/stamp` location」——这是**部署动作**，不是落码动作。
- 因此：**B 现在落码 + 测好；等小叽回传再定部署顺序（先部署 B 看日志，再按需摘 location）。**

---

## 6. 不得改动 / 回归注意
- 保持 `public + X-Stamp-Token` 形态（不改成 `authenticated + record:write`），因为旧机器人在跑、需要无 session 调用。
- B 部署不影响旧机器人（白名单 `127.0.0.1` 放行）。
- 回滚：spec §12（删 auth.db + git 切回）仍有效。
