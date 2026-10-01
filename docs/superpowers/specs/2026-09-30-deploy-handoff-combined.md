# 合并部署交接单：B（stamp 加固）+ v2.5（RBAC 门禁）+ A（灰度开关）

> 给：小叽（服务器助手）｜ 由：芙蕾雅 ｜ 日期：2026-09-30（**定稿**：D4 已合入 commit 286f772，§6 日志器名改为 `app`，含 ds 4 处 + 清旧库前置）
> 本文自包含，无需看其他文档即可执行。仓库/分支/环境变量名以本单为准。
> ✅ **D4 已随 PR #25 合入**（commit `286f772`）：`_dry_run_log` 改用 `current_app.logger`，AUTH_DRY_RUN 行现落到 `logs/app.log`、日志器名 `app`。故 §6 / §7 现已可执行，可按本单观察后翻 `AUTH_ENABLED=1`。

## 0. 一句话目标

本次把 **三件套同批部署**到生产：
- **B**：`/api/stamp` 接口加固（IP 白名单 + 拒代理头 + 强 token 校验）；
- **v2.5**：RBAC 权限门禁（默认**关**）；
- **A**：`AUTH_ENABLED` 灰度开关（默认**关**）。

**初始部署 `AUTH_ENABLED` 不设置（=0）** ⇒ B 的 stamp 加固立即生效，v2.5 门禁关着（只观察不拒绝，dry-run），所有修改 `updated_by` 署 `system`。观察数天后再翻 `=1`。

## 1. 仓库与分支（git pull 目标）

- 仓库：`https://github.com/roke8x-max/yxo-app.git`
- 分支：`main`（PR #25 已合并入 main，含 B+v2.5+A+D4 全部代码与文档）
- ⚠️ 追加项：外部只读账号「游客」+ 禁用 `visitor_demo`（见 §10）随本单后续 commit 合入 main，部署前请先 `git pull` 确保含该提交。
- 拉取：`git fetch origin && git checkout main && git pull --ff-only`

## 2. 部署前置（环境变量）⚠️ 本次为权限模块**首次**上线，以下变量生产从未设过，必须新建

| 变量 | 本次取值 | 说明 |
|---|---|---|
| `YXSTAMP_TOKEN` | **必须设**（强随机值） | B 要求：生产环境若缺失，`import app` 会**直接 raise 拒绝启动**，绝不带弱默认上线。生成：`python -c "import secrets;print(secrets.token_hex(32))"` |
| `YX_AUTH_ENABLED` | **不要设置**（默认 `0`） | 本次初始部署保持关。=0 ⇒ 门禁 dry-run、stamp 加固仍生效。 |
| `YXO_AUTH_SECRET` | **必须设**（强随机长串，≥32 字节） | **首次上线**：生产此前从未设过。不设则 `auth` 模块用进程内随机值，**重启后旧 CSRF token 全部失效 → 翻 `=1` 后登录 POST 直接 403**。翻 `=1` 前必须已设。生成同上 `secrets.token_hex(32)`。 |
| `YXO_AUTH_PASSWORD_MAOXIAOYANG` | **必须设**（各自强随机） | 4 位同事（毛骁洋/冯茜/杨雅雯/韩文豪）初始口令。不设则 `init_auth_db` 写入弱占位口令 `yxo-dev-<key>`，**翻 `=1` 后任何人可登录**。 |
| `YXO_AUTH_PASSWORD_FENGQIAN` | **必须设** | 同上 |
| `YXO_AUTH_PASSWORD_YANGYAWEN` | **必须设** | 同上 |
| `YXO_AUTH_PASSWORD_HANWENHAO` | **必须设** | 同上 |
| `YXO_AUTH_PASSWORD_VISITOR_DEMO` | **建议设** | 测试账号 `visitor_demo` 初始口令；本单将禁用（见 §2.5 步骤 4），仍建议设以防 dev 占位口令。 |
| `YXO_AUTH_PASSWORD_YOUKE` | **必须设** | 真实外部只读账号 `游客` 初始口令（scope=全量 5 家开票子公司，供集团下属数科公司同事看数据）。 |
| `YXO_AUTH_DB` | 默认 `data/auth.db` | 一般不改；见 §2.5 清旧库。 |

⚠️ 切勿在本次部署时设 `YX_AUTH_ENABLED=1`——那会让 v2.5 门禁立即全效，跳过观察期。

### §2.5 阻塞前置（不完成则不得部署 / 不得翻 =1）

- ✅ **（D4，已合入 commit `286f772`）** `_dry_run_log` 已改用 `current_app.logger.info(...)`，AUTH_DRY_RUN 行现可靠落到 `logs/app.log`、日志器名 `app`。§6 观察期已可执行。**本项已满足，仅作记录。**

- ⚠️ **（清旧库，防 dev 弱口令带上线）** `init_auth_db` 幂等：用户已存在则跳过。若部署机 `data/auth.db` 已存在（来自任何旧测试/拷贝，含 dev 弱口令种子），重跑 `init_auth_db` **不会**用环境变量真口令覆盖。故部署前必须：
  1. 删除 `data/auth.db`（`del data\auth.db`）；
  2. 设好上面全部 `YXO_AUTH_PASSWORD_*`（含新增的 `YOUKE`；`VISITOR_DEMO` 见步骤 4 禁用）：
  3. 再跑 `python scripts/init_auth_db.py`（此时用真口令 seed，含新建的 `游客` 账号）。
  4. （禁用测试号）重建后 `visitor_demo` 会以启用态存在，测试账号不应在生产对外可用，执行一次性禁用（幂等，`init_db` 不会回写 `disabled`）：
     `sqlite3 data/auth.db "UPDATE auth_users SET disabled=1 WHERE username='visitor_demo';"`
     未装 sqlite3 CLI 时改用 Python：`import sqlite3; c=sqlite3.connect('data/auth.db'); c.execute("UPDATE auth_users SET disabled=1 WHERE username='visitor_demo'"); c.commit()`
  若不清旧库，翻 `=1` 后 dev 弱口令仍可用，等于没设密码。**此项为真正阻塞前置，必须完成。**

## 3. 启动与冒烟

1. 重启服务（按现有 nssm / 进程方式）。
2. 确认启动**无 raise**（缺 `YXSTAMP_TOKEN` 会启动即报错；`YXO_AUTH_SECRET` 缺失仅 WARNING，不阻断启动）。
3. 健康检查：原公开接口（如 `/api/version`、首页）正常 200。

## 4. 验证 B（stamp 加固，应已生效）

从**本机 127.0.0.1** 用正确 `X-Stamp-Token` 调 `POST /api/stamp`：
- 正确 token → `200`（箱号不存在也只是 `found:false`，证明 token+白名单通过）；
- 错误 token → `403`，响应体为 JSON（`{"ok":false,"error":"HTTP_403",...}`）；
- 非本机 IP（如 `8.8.8.8`）→ `403`；
- 带 `X-Forwarded-For` / `X-Real-IP` 头 → `403`（防 nginx 反代绕过白名单）。

## 5. 验证 A（disabled，应全放行 + 记日志）＋ 部署前通知同事

- **部署前先通知 4 位同事**（毛骁洋 / 冯茜 / 杨雅雯 / 韩文豪）：「系统升级期间，记录修改的 `updated_by` 会暂署 `system`，属预期、无需惊慌；登录功能暂未开启」。此通知对应 §2 前置（secret/密码已就绪）已完成、但门禁尚未开，提前打招呼避免误判。
- 任意受保护接口（如 `POST /api/row`、`GET /api/admin/status`）返回 **200**（无登录/CSRF 要求）；
- 应用日志（部署目录 `logs/app.log`，日志器名 `app`）出现 `AUTH_DRY_RUN path=... method=... ip=... matched=... has_session=False` 行（D4 已合入，该项已具备）；
- 修改一条记录后查库，`updated_by` 列 = `system`（灰度通知口径，属预期）。

## 6. 观察期（≥ 数天，覆盖业务高峰）✅ D4 已合，可执行

**日志位置**：应用**部署根目录**下的 `logs/app.log`（app.py 的 RotatingFileHandler 挂在 `app.logger`，INFO 级）。
单行形如：
`2026-09-30 ... [req_id:xxxx] INFO app: AUTH_DRY_RUN path=/api/row method=PATCH ip=127.0.0.1 matched=record:write has_session=False`
（注意日志器名是 **`app`**，**不是** `auth`——这正是 D4 修复的结果；若搜到的是 `INFO auth:` 说明部署的是旧代码，需重新 `git pull`。）

> 部署根目录 = nssm 服务启动目录（即 `yxo-app/` 根）。若不确定，看 nssm 服务的「路径 / 可执行文件」即可定位；日志就在该目录的 `logs\app.log`。

在**部署根目录**下执行：
- 抓取全部（PowerShell）：`Select-String "AUTH_DRY_RUN" logs\app.log`
- 按本机 IP 过滤（洋要求的可复制命令）：`Select-String "AUTH_DRY_RUN" logs\app.log | Select-String "127.0.0.1"`
- 或（git-bash）：`grep "AUTH_DRY_RUN" logs/app.log` / `grep "AUTH_DRY_RUN" logs/app.log | grep "127.0.0.1"`

拉 `AUTH_DRY_RUN` 日志，做两类过滤：

1. **受保护接口误伤排查**：筛选 `ip=="127.0.0.1"` **且** `matched` ∈ `authenticated` / 具体权限（非 `public`）。
   - 若有命中 → 说明旧 localhost 机器人在调受保护接口，翻 `AUTH_ENABLED=1` 后会被 401 挡死。处理二选一：① 给这些接口加 127.0.0.1 例外；② 先改机器人带合法 session。
2. **`/api/stamp` 单独抽查**：其 `matched==public`，不在上面过滤条件。单独看 `path==/api/stamp` 的 dry-run 行：若 `ip==127.0.0.1` 但带 `X-Forwarded-For` 或频率异常 → nginx 已暴露 `/api/stamp`（B 白名单被绕过）。

> 若 `logs/app.log` 中搜不到任何 `AUTH_DRY_RUN` 行、**或搜到的是 `INFO auth:`（旧日志器名）** → 即 D4 未生效 / 部署的是旧代码，立即重新 `git pull` 并回查启动日志，不得进入 §7。

## 7. 翻 `YX_AUTH_ENABLED=1`（仅当全部满足）

**必须同时满足**才允许设 `YX_AUTH_ENABLED=1` 并重启：
- **D4 已随 PR 合入**（commit `286f772`）：§6 已能搜到 `AUTH_DRY_RUN` 行且日志器名为 `app`；
- **§2 前置全部就绪**：`YXO_AUTH_SECRET` 已设、`YXO_AUTH_PASSWORD_*` 5 个已设、`data/auth.db` 已按真口令重建（删旧 + 重跑 `init_auth_db`）、4 同事已通知（§5）；
- 第 6 节「受保护接口」过滤结果为空（无旧机器人误伤）；
- `/api/stamp` 抽查无异常（或 nginx 已摘 `/api/stamp` 的 location，见 §9）。

## 8. 回滚

- 初始阶段（=0）：回滚只需 `YX_AUTH_ENABLED=0`（或不设）重启，不需删 `auth.db`。
- 翻到 =1 后回滚：同 B/v2.5（删 `auth.db` + git 切回 main）。

## 9. 可选收尾（不影响本次）

- **nginx `/api/stamp` location 摘除**：若第 6 节抽查发现 nginx 把 `/api/stamp` 暴露到公网，需在 nginx 侧摘掉该 location（仅限本机/内网可达）。此步**不前置**，按抽查结果决定。
- 跨域 OPTIONS 预检：当前前端与后端同源，不受影响；若将来引入跨域调用，需补 CSRF 预检豁免（A 验收报告 §6 已记）。

## 10. 外部只读账号「游客」（本次随 RBAC 同批部署）

- **是什么**：真实外部只读账号（角色 `visitor`，仅 `record:read`+`record:export`，无写无管理），供集团下属数科公司同事通过网页看集团全量 5 家开票子公司数据。取代此前的测试账号 `visitor_demo`。
- **怎么来**：已写入 `auth/schema.py` 的 `SEED_USERS`，`init_auth_db`（随服务启动自动跑，见 §3）会幂等建号；口令来自环境变量 `YXO_AUTH_PASSWORD_YOUKE`（见 §2，必须设真实强口令，否则 dev 占位 + WARNING）。
- **`visitor_demo` 处理**：测试账号，本单 §2.5 步骤 4 已将其 `disabled=1` 禁用；它仍保留在 `SEED_USERS` 中仅供开发/测试安装使用，生产上应保持禁用。
- **部署后验证（洋/小叽任选）**：
  - 查账号存在且角色/禁用态正确：`sqlite3 data/auth.db "SELECT username,role,scope_type,disabled FROM auth_users WHERE username IN ('游客','visitor_demo');"` → 应见 `游客|visitor|companies|0` 与 `visitor_demo|visitor|companies|1`。
  - 用 `游客` + `YXO_AUTH_PASSWORD_YOUKE` 的口令登录网页，确认只能看（不能改）全量 5 家数据。
