# 转发 OpenCode · PR-B 价格维护加固（2026-10-12 预写 · 待 PR-A 合并后再发）

> 任务书：`docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` PR-B 节
> 状态：**定稿待发** —— 等 PR-A 收口后再发（五件串行：**A → B → E → C → D**）
> 芙蕾雅核定版

---

OpenCode，任务书 `docs/mainsite/taskbooks/2026-10-12-主站四件套-taskbook-OpenCode.md` 已拍板消项，**本轮只做 PR-B · 价格维护加固（B①–B⑤）**。
分支名 `fix-price-admin-hardening`（仅供芙蕾雅建分支用，**你不用做任何 git 写操作**，含 `checkout -b`）。

严格按任务书 **PR-B 节**执行（B①–B⑤ 五小项都要做，见任务书；含 B④ 的 N=30 与「价格表」卡片底部恢复入口、B⑤ 的审计表）。

## ⛔ 范围红线（本 PR 有两处高危交叉点 —— 这是最容易犯规的地方）

1. **B① 会打开 `app.py` 的 `/api/price/batch`**（= 登记项 **R3** 的锚点 `app.py:922-945`）。
   → **只加「读价失败 → 就地报错、跳过写库」的 catch**；
   → **不改**这个函数的查询语义 —— **退舱守卫、`is_deleted` 过滤一律不碰**（R3 只登记，不修）；
   → 「不写库」= **读价失败时不要落库**，**不是**让你删掉正常路径的算价/写入逻辑。
2. **B⑤ 会打开 `admin_api.py` 的 `api_price_recalc`**（= 登记项 **R4** 的锚点 `admin_api.py:1109-1111`，**同一个函数**）。
   → **B⑤ 只加审计日志**，**不改**该函数的查询与守卫逻辑（R4 只登记，不修）。

👉 **你在 `app.py:922-945`（R3 锚点）与 `admin_api.py:1109-1111`（R4 锚点，同属 `api_price_recalc` 函数体）里都会看到「退舱行 / 已软删行被批量写入」的缺口 —— 只登记，不修。**
登记表在任务书第 3 节（R1–R4）。

## 明确不做
任务书第 3 节登记表 **R1–R4 全部不要顺手做**（退舱单元格编辑限位 / 启动补导入 / 两处价格端点退舱缺口）。

> 🔖 **但要知道**：**R3/R4 已由毛骁洋拍板「本期修」**，将在 **PR-B 之后的独立 PR-E**（`fix-price-batch-cancel-guard`）处理 —— 所以你在本 PR 里仍然**只登记、不修**，E 会接手。别以为登记项被撤销了，也别替 E 提前动手。

## 注意：两处实现必须同步改（任务书已注明）
- **B②**：`app.py:415-416` 与 `admin_api.py:854-855` **两处**的 `if rp is None and ct == "COC": rp = pi.get("SOC")` 都要删 —— 只改一处会导致同一单在不同入口算出不同的价。
- **B①**：`admin_api.py:810-816` 与 `app.py:378-384` **两处**读价函数都要改抛。

## CSRF 提示
`admin_bp` 的写端点（`price/exrate`、`price/entry`、`price/restore` 等）与主站写请求都受全局 `before_request` CSRF 校验（`auth/__init__.py:92`，头名 `auth/service.py:21` `X-CSRF-Token`）——写测试时别漏，否则 403。

## 完成后的报告必须含
- 改了哪些文件 + `文件:行号`；
- 实跑命令 + **输出末几行**；
- 证据两步：① 本 PR 新增测试单跑绿；② **全量** `0 failed`（`& $PY -m pytest -q -p no:cacheprovider`，`$PY` = 本机 python.exe 绝对路径，先自检 `& $PY -c "import sys;print(sys.executable)"`；输出**不要**重定向到仓库内文件，要存就存 `%TEMP%`）；
- 逐条对应任务书 PR-B 验收 1–7 的结论；
- 计划外发现：**只登记，不顺手修**。

不做任何 git 写操作（`add` / `commit` / `push` / 开 PR / 建分支都由芙蕾雅来）。
