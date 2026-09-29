# B 部署核对单 · /api/stamp 加固

- **日期**：2026-09-29
- **对象**：B 工单（`2026-09-29-taskbook-B-stamp-hardening.md`）落码已验收通过（23 passed + 全仓 509 passed/EXIT=0）
- **执行人**：洋 / 小叽（在服务器 `10.0.199.184` 上操作）；芙蕾雅不在远端，负责出单与回传解读
- **状态**：待执行。步骤 5 为**条件分支**，仅依赖小叽三查回传结果（`docs/superpowers/specs/2026-09-29-handoff-xiaoji-stamp-check.md` 的回复）。
- **原则**：先记录、不提前摘；小叽回传只决定「步骤 5 摘不摘 location」，**不影响任何落码/部署动作**。

---

## 前置 · 部署前必做

- [ ] **设 `YXSTAMP_TOKEN`**：在服务器生产环境（config_local / 进程 env）设一个**强随机**值，覆盖默认。
  - ⚠️ 未设时服务启动应直接 `raise RuntimeError`（见步骤 2），这是安全门禁，不是 bug。
  - 旧 DSK/ATB 机器人（`mailbots/Dsk_Robot.py`、`Atb_Robot.py`）也从此处读同一 token，需保持一致，否则旧机器人会被 403 挡死。
- [ ] **确认提交清单（禁用 `git add -A`）**：
  - 本部署范围 = B 改动：`app.py`(stamp 函数体 hunk)、`config.py`(STAMP_TOKEN 行)、`auth/common.py`(新建)、`auth/__init__.py`(raise)、`auth/rbac.py`(注释)、`tests/auth_gate_test.py`(7 个新测试)。
  - 工作区当前**叠着主 RBAC 模块落码**（auth/ 整包未跟踪、app.py 的 g.identity 改动、admin_api.py/static/app.js/templates/* 前端收口、config.py 的 AUTH_* 配置），以及**尚未落码的 A 工单改动（无）**。
  - 提交时用**显式文件列表**逐个确认属于本部署；`git add -A` 会把未跟踪的 auth/ 整包与未就绪改动一并卷进同一 commit，严禁。
  - 建议：主 RBAC 模块与 B 可同批提交（均已验收），但必须**列清单逐文件 add**，不要 `-A`。

---

## 步骤 1 · 记录现状（只记录，不摘）

目的：留基线，供步骤 4/5 对比。

- [ ] **记录旧机器人现状**：在服务器确认 `Dsk_Robot` / `Atb_Robot` 进程是否仍在跑（任务管理器 / `Get-Process` / 调度任务）。记录 PID 与命令行（确认它用 `127.0.0.1:5011` 直连、token 来源）。
- [ ] **记录 nginx 现状**：读取 nginx 配置，确认 `/api/stamp` 是否有 `location` 块 `proxy_pass` 到 `127.0.0.1:5011`。**只记录结论（暴露 / 未暴露），此刻不修改 nginx。**

> 说明：此步即小叽三查中的①②。若小叽已回传，直接采用其结论；未回传则本步由执行人在服务器现场完成。

---

## 步骤 2 · 部署后确认服务启动

- [ ] 拉取 B 改动、按前置清单提交、重启 yxo-app 服务（nssm / 进程重启）。
- [ ] **验证启动门禁**：
  - 若 `YXSTAMP_TOKEN` **未设** → 服务应**启动即 `raise RuntimeError("YXSTAMP_TOKEN 未设置…")`**、进程退出。这是预期行为，说明缺失拒启生效。补设后重启用。
  - 若已设 → 服务正常起来，日志无 `raise`。
- [ ] 确认 `import app` 启动自检通过（60 路由全登记、无漏登）。

---

## 步骤 3 · 三验证（本机 / 非本机 / 旧机器人）

- [ ] **本机验证（应 200）**：在服务器本机 `curl http://127.0.0.1:5011/api/stamp -H "X-Stamp-Token: <正确token>"` → 返回正常（写 DSK/ATB 时间戳）。
- [ ] **非本机验证（应 403）**：从**外部机器 / 非 127.0.0.1** 调同一接口（带正确 token 也罢）→ 应返回 `403`（白名单 + 拒代理头生效）。
- [ ] **带代理头验证（应 403）**：从本机但带 `X-Forwarded-For` / `X-Real-IP` 头调 → 应 `403`（防 nginx 反代绕过）。
- [ ] **旧机器人验证（应正常）**：观察旧 `Dsk_Robot` / `Atb_Robot` 实际调用 → 应成功写时间戳（它 localhost 直连、不带代理头、带正确 token，三条件全满足）。**若旧机器人被 403 → 立刻回查 token 是否一致（前置项），不要动白名单。**

---

## 步骤 4 · 观察 nginx access log（外部 403 检测）

- [ ] 部署后观察一段时间（建议 ≥ 数小时覆盖业务高峰）。
- [ ] 查 nginx `access.log`（**非 error.log**）：`grep "api/stamp" access.log | grep 403`。
  - 若出现外部 IP 的 403 → 证明 nginx 在暴露 `/api/stamp`（外部请求经代理进来被白名单/拒代理头挡掉）。
  - 若只有 127.0.0.1 的 200 → 无外部暴露，理想。

---

## 步骤 5 · 条件摘 location（唯一依赖小叽回传的步骤）

```
IF 小叽三查回传 或 步骤1/步骤4 显示 nginx 已暴露 /api/stamp：
    → 在 nginx 侧摘掉 /api/stamp 的 location 块（纵深清理），reload nginx。
    → 摘后复查 access.log 不再有该路径请求。
ELSE（nginx 未暴露）：
    → 不摘，保持现状。B 的白名单 + 拒代理头已足够。
```

- [ ] 按上述分支执行（或显式确认「不执行」）。
- ⚠️ **不要把「摘 location」写成部署前置**——先部署 B、看步骤 4 的 403 观察窗口，再决定摘不摘。提前摘会丢掉最宝贵的暴露证据。

---

## 步骤 6 · 回滚预案

若 B 引发问题（旧机器人断、误 403 等）：

- [ ] **代码回滚**：`git revert` B 相关 commit（或 `git checkout <前提交> -- <B文件列表>`），重启服务即回退到加固前（public+弱默认形态）。
- [ ] **数据无影响**：B 只改 `/api/stamp` 鉴权逻辑，不碰 `auth.db` / `yxo.db`，回滚无数据风险。
- [ ] **快速止血（不回滚代码）**：临时在 `config_local` 把 `YXSTAMP_TOKEN` 设回与旧机器人一致、或临时放行——仅限应急，事后仍走正式回滚。
- [ ] 回滚后通知小叽停止相关观察。

---

## 执行记录（填此处）

| 步骤 | 操作人 | 时间 | 结果 / 备注 |
|---|---|---|---|
| 前置 | | | |
| 1 | | | 旧机器人：___ 在跑/不在；nginx：___ 暴露/未暴露 |
| 2 | | | 启动门禁：___ 设token正常 / 未设raise(已补) |
| 3 | | | 本机200 / 非本机403 / 代理头403 / 旧机器人正常 |
| 4 | | | access.log 外部403：___ 有/无 |
| 5 | | | ___ 摘location / 不摘 |
| 6 | | | 未触发 / 已回滚 |

---

**B 稳定后**：再拆 A 工单（`AUTH_ENABLED` 开关 + dry-run + 方案1 system identity），按「部署后 `AUTH_ENABLED=0` 关着跑 dry-run 观察」推进。
