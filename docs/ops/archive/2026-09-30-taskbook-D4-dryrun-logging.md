# D4 工单：dry-run 观察日志丢失（阻塞项）

> 给：OpenCode ｜ 由：芙蕾雅 ｜ 日期：2026-09-30
> 自包含，无需看其他文档即可落码。验收归芙蕾雅（必须实证，不能只看代码）。
> 提交目标分支：`rbac-v2.5-stamp-b`（即 PR #25，合并部署交接单把本修复列为「§2.5 阻塞前置」）。

## 0. 背景（缺陷与实证）

`auth/__init__.py` 的 `_dry_run_log(req)`（disabled 期观察日志，前缀 `AUTH_DRY_RUN`）
当前用模块级 `logger = logging.getLogger("auth")`（第 19 行定义、第 70 行使用）。

但 `app.py:67-84` 把 RotatingFileHandler 挂在了 **`app.logger`**（Flask 应用日志器）上，
文件名 `logs/app.log`（INFO 级）。`"auth"` 日志器的祖先**只有 root**，并不经过 `app.logger`，
而 root 在本案无 handler → 该 INFO 行被**静默丢弃**。

**芙蕾雅已用最小复现脚本实证**（复刻 app.py 的 logging 配置）：
- `logs/app.log` 内容为空；
- `auth` 日志器 `propagate=True`、自身 handler 数 = 0；
- root 日志器 handler 数 = 0。

**后果**：合并部署交接单 §6「观察期」目前根本捞不到日志，无法判断是否误伤旧 localhost 机器人，
翻 `AUTH_ENABLED=1` 会变成盲翻。本修复是合并部署的**阻塞前置**。

## 1. 修改点（`auth/__init__.py`）

### 1.1 改 import（第 12 行附近）
```python
from flask import g, jsonify, request, current_app
```
（原 `from flask import g, jsonify, request`，补 `current_app`。）

### 1.2 改 `_dry_run_log`（第 63-71 行）
把
```python
    logger.info("AUTH_DRY_RUN path=%s method=%s ip=%s matched=%s has_session=False",
                req.path, req.method, req.remote_addr, matched)
```
改为
```python
    current_app.logger.info("AUTH_DRY_RUN path=%s method=%s ip=%s matched=%s has_session=False",
                            req.path, req.method, req.remote_addr, matched)
```
`_dry_run_log` 由 `_auth_gate`（before_request）调用，处于应用上下文内，`current_app` 可用。
此写法与同文件 `init_auth` 内既有的 `app.logger.warning(...)`（第 142/144 行）一致。

### 1.3 清理不再使用的引用
第 19 行 `logger = logging.getLogger("auth")` 改为 `current_app.logger` 后**不再被任何代码使用**，删除。
第 10 行 `import logging` 经 grep 确认仅服务于第 19 行，删除后全文件无其它 `logging.` 用法，一并删除。
（若删除后发现其它地方用到 `logging`，以 grep 为准保留——但本包内已确认无。）

## 2. 顺手排查同款坑（已替你 grep，结论如下，无需重查）

对 `auth/` 整个包做 `getLogger|logger|print|import logging|logging.` 全量 grep：

- `auth/__init__.py:70` —— 唯一用到 `getLogger("auth")` 之处（即本缺陷），已修。
- `auth/__init__.py:142 / 144` —— 用 `app.logger.warning(...)`，正确（落 `logs/app.log`），**不动**。
- `auth/service.py:161` —— 用 `print("[auth] WARNING: YXO_AUTH_SECRET 未设置...")`。`print` 走 stdout，
  由 nssm 捕获到服务日志，**不是**「丢失」同款坑，**不动**（如需统一可另开单，不在本工单范围）。
- `auth/schema.py:92` —— 用 `print(f"[auth] WARNING: YXO_AUTH_PASSWORD_... 未设置...")`，同上，**不动**。
- `auth/audit.py` —— 无任何 `logger` / `print` / `logging` 用法，**无需处理**。

> 结论：本工单只需改 `_dry_run_log` 一处（§1.2）+ 清理 §1.3，无其它同款坑。

## 3. 验收（必须实证，不能只看代码）

### 3.1 新增测试 `tests/auth_gate_test.py::test_dry_run_log_lands_in_app_log`
沿用既有 `dclient` fixture（AUTH_ENABLED=0，disabled 态）：

```python
def test_dry_run_log_lands_in_app_log(dclient):
    from app import app as _app
    import os
    from logging.handlers import RotatingFileHandler
    # 从 handler 取真实日志路径（不要自己拼目录约定，避免与 _LOG_DIR 不一致而对不上）
    log_path = next(h.baseFilename for h in _app.logger.handlers
                    if isinstance(h, RotatingFileHandler))
    # —— 防「假通过」：日志是追加写入，若文件里已有旧的 AUTH_DRY_RUN（调试跑过 / A 工单测试写过），
    # 仅看 "AUTH_DRY_RUN" in content 会误判通过。先记 size_before，本次请求后必须增长。
    size_before = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    # 触发一次非噪声请求（disabled 态 _auth_gate 注入 system 并调 _dry_run_log）
    dclient.get("/api/rows")  # 任意已知端点均可；GET 也会触发 _dry_run_log
    # flush 必须针对 app.logger 上的 handler —— 不要用 root.handlers！
    # RotatingFileHandler 挂在 app.logger 上（app.py:67-84），root 没有这个 handler，
    # logging.getLogger().manager.root.handlers 是空列表，flush 它毫无作用。
    # （FileHandler 默认同步落盘，flush 是双保险；若不想 flush 也可直接删掉本段。）
    for h in _app.logger.handlers:
        h.flush()
    size_after = os.path.getsize(log_path)
    assert size_after > size_before, "日志文件未增长，AUTH_DRY_RUN 未真写入（D4 修复未生效？）"
    # 读文件断言内容（errors="ignore" 兜底：handler 已指定 encoding="utf-8"(app.py:73)，
    # 但加 ignore 防 Windows 编码读取异常导致解码崩溃）
    content = open(log_path, encoding="utf-8", errors="ignore").read()
    assert "AUTH_DRY_RUN" in content, "disabled 期观察日志未落到 logs/app.log"
```

> 说明：`app.py` 在 import 时已按 `_LOG_DIR = <仓库根>/logs` 挂好 RotatingFileHandler（在
> `app.logger` 上，且 `encoding="utf-8"`，见 app.py:71-73），测试 import app 即完成配置；
> 请求在 disabled 态必定写一行 `AUTH_DRY_RUN`。
> 三条常见误写（均已修）：① flush 写成 `logging.getLogger().manager.root.handlers`（root 无
> 该 handler，flush 无效）；② 日志路径用 `os.path.dirname(_app.__file__)+"logs/app.log"` 自拼
> （依赖目录约定，配置一迁移就对不上）；③ 仅断言 `"AUTH_DRY_RUN" in content`（文件里若有旧的
> 调试/A 工单日志行会假通过）——本测试额外用 `size_before/size_after` 增长断言自证「本次真的写入」。

### 3.2 既有断言不得删
`tests/auth_gate_test.py` 既有 31 条断言一个不删；本测试为新增。

### 3.3 全仓回归
`pytest -q` 仍全绿（当前基线 517）。

### 3.4 验收证据（必附，不能只贴「测试通过」）
落码后提交时必须附**真实证据**，芙蕾雅要核对日志是真落盘、不是「路径恰好拼对 + flush 恰好没用」偶然过的：
1. 贴 `pytest tests/auth_gate_test.py::test_dry_run_log_lands_in_app_log -v` 的输出（passed）；
2. **贴 `logs/app.log` 里 `AUTH_DRY_RUN` 那行的真实内容**（grep 出来整行），
   形如：`2026-09-30 ... INFO auth: AUTH_DRY_RUN path=/api/rows method=GET ip=127.0.0.1 matched=... has_session=False`。
   缺这行真实日志，视为验收不通过。

## 4. 不得触碰范围
- 只改 `auth/__init__.py`（§1）与 `tests/auth_gate_test.py`（§3.1）。
- **不碰** `app.py` 的 logging 配置、`auth/service.py`、`auth/schema.py`、`records_dao.py`。
- 不引入任何新依赖。

## 5. 提交
- 分支：`rbac-v2.5-stamp-b`（PR #25，与 B+v2.5+A 同批）。
- message 建议：`fix(auth): D4 dry-run 日志改用 current_app.logger 落到 logs/app.log`。
- 不 push 到 main，不 merge；由芙蕾雅验收后再走合并部署流程。
