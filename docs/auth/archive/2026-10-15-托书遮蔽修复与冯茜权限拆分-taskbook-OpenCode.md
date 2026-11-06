# 任务书：托书遮蔽 bug 修复 + 冯茜算价权限拆分（pull 前必修）

- 日期：2026-10-15
- 发起：芙蕾雅（依据小叽 2026-10-14 工单 + 毛骁洋 2026-10-14 拍板）
- 执行：OpenCode（只落码；commit / push / 开 PR 由芙蕾雅接手）
- 代码基线：main `34cfdb6`（本地 yxo-app 已同步到该提交）
- 测试环境：小叽 `F:\YXO_TEST`（5012），小叽负责修复后复测

## 背景（为什么改，自包含）

### 问题 1：托书生成必坏

`app.py` 的 `api_tuoshu_generate()` 里，两处 `for` 循环的循环变量叫 `g`，与 Flask 请求上下文对象 `g` 重名。循环结束后，函数末尾 `_audit.log_event("生成托书", g.identity.username, ...)` 读到的 `g` 已经是班列字典（dict），报 `'dict' object has no attribute 'identity'`，HTTP 500。

实测：凡有 `tuoshu:generate` 权限的账号（毛骁洋 / 杨雅雯 / 韩文豪）点「生成托书」全部 500。小叽已在 F:\YXO_TEST 复现，并按本任务书的改法修好、复验通过（三账号 200、冯茜 403 预期、审计署名正确）。

### 问题 2：前台算价与后台价格维护共用一个权限

`auth/rbac.py` 里 `POST /api/price/<int:rid>`（前台单条算价）、`POST /api/price/batch`（前台批量算价）与后台 7 个 `/api/admin/price/*` 路由全部登记为 `price:manage`。给冯茜 `price:manage` 她就能进后台改报价——违背洋的要求。

**洋 2026-10-14 拍板**：
1. 冯茜要能在主表算价，**不能**进后台价格维护 ⇒ 新增 `price:apply` 给她；`price:manage` 不给。
2. 杨雅雯 / 韩文豪**权限一律不动**（仍持有 `price:manage`，保留后台改价能力）。有意为之，不要"顺手统一"。
3. 冯茜同时加 `tuoshu:generate`（托书权限名单扩为冯茜 + 杨雅雯 + 韩文豪）。

## 分支与流程

- 分支名：`fix-tuoshu-g-shadow-price-apply`（扁平名，从最新 main 切出）
- 全程遵守 `WORKFLOW.md` §3.1 七步分支循环；**不要动 main**
- OpenCode 只在分支上写码 + 跑测试；commit/push/PR 由芙蕾雅做

---

## 任务 A：修复 `g` 遮蔽（app.py，行号基于 34cfdb6）

### A1. 第一处循环（app.py:1224-1230）改名

```python
# 原
            new_groups = []
            for g in groups:
                st = sel.get((g["train_no"], g["departure_date"]))
                if st is None:
                    continue
                g = dict(g)
                g["station_cn"] = st   # 用用户选定的目的站生成
                new_groups.append(g)
            groups = new_groups

# 改
            new_groups = []
            for grp in groups:
                st = sel.get((grp["train_no"], grp["departure_date"]))
                if st is None:
                    continue
                grp2 = dict(grp)
                grp2["station_cn"] = st   # 用用户选定的目的站生成
                new_groups.append(grp2)
            groups = new_groups
```

注：紧随其后的 app.py:1235-1236 列表推导式里的 `g` 是推导式局部作用域（Python 3），**不会**遮蔽外层 `g`，可不动。但为了一致易读，也可顺手改成 `grp`，二选一，不强制。

### A2. 第二处循环（app.py:1254-1258）改名

```python
# 原
        for g in groups:
            files.append(eng.generate_one(
                {"train_no": g["train_no"], "departure_date": g["departure_date"],
                 "station_cn": g["station_cn"], "boxes": g["boxes"]},
                booking_date, out_dir))

# 改（g → grp，四处全部替换）
        for grp in groups:
            files.append(eng.generate_one(
                {"train_no": grp["train_no"], "departure_date": grp["departure_date"],
                 "station_cn": grp["station_cn"], "boxes": grp["boxes"]},
                booking_date, out_dir))
```

### A3. 操作者提前取出（防御，app.py:1263 与 1272）

在 `try:`（app.py:1249 附近）之前、`if not os.path.exists(tpl_path)` 检查之后加一行：

```python
    # 操作者取自服务端 session（g.identity）。不要在循环内或循环后再取——
    # 若局部变量与 Flask 的 g 同名会遮蔽身份对象（本次 bug 的根因）。
    operator = g.identity.username
```

然后把 1263 行改为：

```python
            _audit.log_event("生成托书", operator, detail=f"train={groups[0]['train_no']}")
```

1272 行改为：

```python
        _audit.log_event("生成托书", operator, detail=f"groups={len(groups)} zip")
```

### A4. 全文排查同类遮蔽 + 防再发

1. **既有遮蔽点的处置口径（芙蕾雅已用递归展开版扫描实测，2026-10-15，基线 34cfdb6）**：扫描 `app.py` + `auth/*.py` 共命中 **9 处**，全部按"机械改名修掉"处理，**不允许为通过测试而缩小检查范围或加豁免清单**：
   - `api_tuoshu_generate` 4 处（1224/1228/1254 的 `g`，即 A1-A3 修复范围）；
   - `_tuoshu_groups` 3 处（1085/1087 赋值、1102 循环的 `g`）——已核实该函数内**不使用** Flask `g`（无 `g.identity` 等），不构成运行时 bug，但按新口径一并改名 `grp`（纯卫生，函数只被 `api_tuoshu_generate`/preview 内部调用，改名无外部影响）；
   - auth/ 3 处**函数参数**叫 `app`：`auth/common.py:10 is_production(app)`、`auth/__init__.py:119 _self_check(app)`、`148 init_auth(app)`——这是 Flask 扩展惯用写法，不改调用方，**只把参数名机械改为 `flask_app`**（已核实三处调用均为位置传参：app.py:1695 `_init_auth(app)`、auth/__init__.py:163 `_self_check(app)`、tests:349 `init_auth(prod)`，改名无影响）。⚠️ **改名必须连带函数体内所有对 `app` 的引用一并改**，只改参数名会直接 NameError。已核实的引用清单：`is_production` 内 2 处 `getattr(app, ...)`；`_self_check` 内 `app.url_map` ×2、`app.debug`、`app.logger` ×2；`init_auth` 内 `app.secret_key` ×2、`app.register_blueprint`、`app.before_request`，以及**内部传参调用** `is_production(app)` / `_self_check(app)` 两处的实参也要跟着变成 `flask_app`。
   - 其余 `for g in`（如 app.py:1189-1190）是推导式/生成器目标，Python 3 独立作用域，不在命中范围。
   你落码时请以实际代码为准复核，与上面清单有出入就在交付说明里指出。
2. **测试实现要求（`tests/test_no_shadow.py`）**：用 `ast` 解析 `app.py` + `auth/*.py`，断言**函数作用域**内不存在名为 `g`、`request`、`session`、`app` 的局部绑定。检查范围：
   - `FunctionDef` / `AsyncFunctionDef` 的参数（`args.args` / `args.kwonlyargs` / `args.posonlyargs` / `args.vararg` / `args.kwarg`）
   - `Assign` / `AnnAssign` / `AugAssign` 的目标
   - `For` / `AsyncFor` 的 target
   - `With` / `AsyncWith` 的 `optional_vars`
   - `ExceptHandler` 的 name
   - `NamedExpr`（海象表达式）的 target
   - ⚠️ **target 必须递归展开**：`for g, x in items` 的 target 是 `Tuple`、`a, b = ...` 是 `Tuple`、`a = b = g` 是多 target——只查单个 `ast.Name` 会漏。对 `Tuple`/`List`/`Starred` 递归收集其中所有 `Name`。
   - **排除**列表推导式 / 生成器表达式的目标（Python 3 有独立作用域，不遮蔽外层）
   - 模块顶层与类体不在函数作用域内，跳过
   - **扫描范围**：动态判定——所有源码含 `import flask` / `from flask import` 的 `.py` 文件（测试里读文件头判断即可），且 `app.py`、`auth/*.py` 必须始终在内。这样将来引入 flask 的模块自动被罩住，不碰 flask 的纯引擎模块不被误伤。
   该测试在 A1-A3 + 本条改名全部落地后必须通过；今后任何把框架对象名用作局部变量的改动会被它拦下。这比配 ruff 规则更直接（ruff 的 builtins 规则管不到 `g` 这种自定义名）。

---

## 任务 B：拆分 `price:apply`（auth/rbac.py + 前端 + 测试）

### B0. ⚠️ 关键设计点（小叽工单未覆盖，芙蕾雅已定口径）

若只把前台两条路由改成 `price:apply`，**杨雅雯 / 韩文豪会丢失前台算价能力**——他们只有 `price:manage`（洋拍板"权限一律不动"），改路由后前台 `/api/price/*` 他们就 403 了。

⇒ 落法：**`ROUTE_PERMISSIONS` 的值从"单个权限串"扩展为"权限串或权限串元组，元组=任一满足即放行"**。前台两条路由登记为 `("price:apply", "price:manage")`——冯茜凭 `price:apply` 通过，杨/韩凭原有 `price:manage` 通过，两边都不动权限。

### B1. `auth/rbac.py` PERMISSIONS（约 27-42 行）

```python
    "price:manage": "价格维护（后台改报价源数据）",   # 语义收窄，仅改描述
    "price:apply":  "按报价表算价（前台填写单箱价格）",  # 新增，插在 price:manage 之后
```

### B2. `auth/rbac.py` ROLE_PERMS（约 54-69 行）

| 角色 | 改动 |
|---|---|
| `admin`（毛骁洋） | **新增** `price:apply`（14 项 → 15 项）；其余不动 |
| `fengqian`（冯茜） | **新增** `price:apply`、`tuoshu:generate`（5 项 → 7 项）；`price:manage` 仍不给 |
| `hanwenhao` / `yangyawen` | **一律不动**（仍 7 项，含 `price:manage`；**不加** `price:apply`） |
| `visitor` | 不动 |

### B3. `auth/rbac.py` 路由权限登记表（约 112-113 行）+ 权限判定扩展

```python
    ("POST", "/api/price/<int:rid>"): ("price:apply", "price:manage"),
    ("POST", "/api/price/batch"):     ("price:apply", "price:manage"),
```

后台 7 条 `/api/admin/price/*`（145-151 行）保持 `"price:manage"` 单值不动。

判定逻辑改动共 **3 个落点**（值从 str 扩展为 str|tuple 后，现有消费点必须逐一适配，漏一处就是全用户 403）：

1. **严格模式判定（`auth/__init__.py:99-115`）**：现状 `perm = ROUTE_PERMISSIONS[key]` 后直接 `perm not in (ident.permissions or [])` —— 值为元组时该判断恒为 True（元组不会出现在字符串列表里），**所有用户全部 403**。必须改为：

```python
    perms = perm if isinstance(perm, (tuple, list)) else (perm,)
    # PUBLIC / AUTHENTICATED 是字符串哨兵，元组值不可能是它们，比较逻辑不受影响
    if any(p == PUBLIC for p in perms): ...        # 原 perm == PUBLIC 分支
    if any(p == AUTHENTICATED for p in perms): ... # 原 perm == AUTHENTICATED 分支
    if not any(p in (ident.permissions or []) for p in perms):
        return _deny("HTTP_403", "无权限", 403)
```

2. **影子期观察日志（`auth/__init__.py:63-71 _dry_run_log`）**：现状把值原样打进 `matched=%s`，元组会打成 `('price:apply', 'price:manage')` 难读。加个小格式化：str 原样、tuple/list 用 `|` 连接，输出 `matched=price:apply|price:manage`。
3. **其他引用点**：改完全仓搜一遍逐一确认（自检 `_self_check`、测试、可能的文档生成）：

```bash
grep -rn "ROUTE_PERMISSIONS" --include="*.py" .
```

（已核：`ROUTE_PERMISSIONS_SET` 只含键、与值类型无关，不用动；tests/auth_gate_test.py:270 也只查键成员。）

### B4. 种子表无需手工迁移（已核实）

`auth/__init__.py:43` 权限是运行时从 `rbac.ROLE_PERMS` 现算的；`auth_role_permissions` 表只在启动 seed 时 `INSERT OR IGNORE`（schema.py:133-135），新权限会自动补进表里。**不需要任何 SQL 迁移**。注意：已在登录态的用户若权限是随 session 缓存的，需重新登录后生效——验收时如有 403 先重登再判。

### B5. 前端显隐（static/app.js）

1. **托书按钮**（app.js:2466-2467）已按 `hasPerm("tuoshu:generate")` 显隐——冯茜加了权限后自动显示，**无需改**。
2. **批量算价按钮**（app.js:1508 附近，`fetch("api/price/batch")`）：目前**没有任何权限判断**，所有账号都能看到、游客点击得 403。改为按 `hasPerm("price:apply") || hasPerm("price:manage")` 显隐（杨/韩只有 `price:manage` 也要能看到）。找到该按钮的创建/渲染位置加判断，与 adminBtn/tuoshuBtn 同款写法。
3. **单条算价入口**（行内改价的保存动作，走 `/api/price/<int:rid>`）——**明确指令，不许沉默跳过**：在 `static/app.js` 里 grep `/api/price/` 的调用点，确认单条算价保存动作是否有 `hasPerm` 显隐判断。**有** → 改成 `hasPerm("price:apply") || hasPerm("price:manage")` 双权限口径；**完全没有显隐逻辑**（如编辑弹窗内直接保存）→ 不新增前端判断，但**必须在交付说明里写明**"单条算价无前端显隐入口，后端已放行两权限，未新增前端判断"。两种情况都要留痕，禁止只答"确认没有"就完事。

### B6. 测试更新（tests/auth_gate_test.py:551-556）

现状断言（需更新）：

```python
    for p in ("manifest:apply", "price:manage", "config:manage", "data:import"):
        assert p in rbac.ROLE_PERMS["admin"]
        assert p not in rbac.ROLE_PERMS["fengqian"]      # 保持成立，不用改
        assert p not in rbac.ROLE_PERMS["visitor"]
    assert "tuoshu:generate" not in rbac.ROLE_PERMS["fengqian"]   # 555 行，要反转为 in
    assert "price:manage" not in rbac.ROLE_PERMS["fengqian"]      # 556 行，保持
```

改：

- 555 行反转为 `assert "tuoshu:generate" in rbac.ROLE_PERMS["fengqian"]`
- 新增：`assert "price:apply" in rbac.ROLE_PERMS["fengqian"]`、`assert "price:apply" in rbac.ROLE_PERMS["admin"]`、`assert "price:apply" not in rbac.ROLE_PERMS["hanwenhao"]`、`assert "price:apply" not in rbac.ROLE_PERMS["yangyawen"]`、`assert "price:manage" not in rbac.ROLE_PERMS["fengqian"]`
- 新增路由断言：`rbac.ROUTE_PERMISSIONS[("POST", "/api/price/<int:rid>")]` 与 `/api/price/batch` 等于 `("price:apply", "price:manage")`；后台 7 条仍为 `"price:manage"`
- **新增 matched 格式断言（对应 B3 落点 2）**：单权限路由的日志输出**保持原样**（`matched=record:write`，tests/auth_gate_test.py:432 既有断言继续通过即证明）；多权限路由（`/api/price/batch`）影子期 dry-run 日志输出 `matched=price:apply|price:manage`
- **新增严格模式行为断言（对应 B3 落点 1）**：开闸状态下，仅持 `price:manage` 的用户 POST /api/price/batch 非 403（任一放行生效）；两权限都没有的用户同路由 403（默认拒绝未被放宽）
- **新增冻结断言（洋拍板"杨/韩权限一律不动、游客不动"，从文档承诺升级为测试强制）**：全量枚举比对，能抓到任何一项的悄悄变动（含"顺手统一"）：

```python
_FROZEN_YH = frozenset(["record:read", "record:write", "record:export",
                        "manifest:import", "tuoshu:generate",
                        "price:manage", "train:manage"])   # 与 34cfdb6 现状逐字一致
assert frozenset(rbac.ROLE_PERMS["yangyawen"]) == _FROZEN_YH
assert frozenset(rbac.ROLE_PERMS["hanwenhao"]) == _FROZEN_YH
assert frozenset(rbac.ROLE_PERMS["visitor"]) == frozenset(["record:read", "record:export"])
```

（不冻结 fengqian / admin——他们本轮有新增、未来也可能随需求演进，冻结会误拦后续改动。）
- **HTTP 层回归用例（硬要求，非可选）**：在 tests/auth_gate_test.py 补 4 条开闸状态下的 HTTP 层断言——① 冯茜 POST /api/price/batch 非 403 ② 冯茜 GET /api/admin/price 403 ③ 杨雅雯 POST /api/price/batch 非 403 ④ 杨雅雯 GET /api/admin/price 非 403（回归：证明没被误伤）。测试库无数据也成立——只验权限门，不验业务结果（非 403 的 4xx/业务报错均可接受，只要不是权限拦截）。

---

## 不得回退 / 不要顺手改的清单

- 杨雅雯 / 韩文豪的 ROLE_PERMS **一行都不能动**（洋拍板"一律不动"）。
- 后台 7 条 `/api/admin/price/*` 的权限**保持 `price:manage`**，不要改成多权限。
- 游客权限不动（2 项）。
- 托书函数里除 A1-A3 指定行外，不要重构其他逻辑（`_tuoshu_groups`、模板加载、zip 打包都不碰）。
- B0 的多权限元组机制只影响 `ROUTE_PERMISSIONS` 值的消费方，不要扩大到 ROLE_PERMS 或 PERMISSIONS 的结构。
- `tests/`、`docs/` 以外的文件不要新增；不引入新依赖（AST 用标准库）。

## 验收清单（完成后交回芙蕾雅）

0. **转小叽时的口径说明（防止复测误判）**：杨雅雯 / 韩文豪仍能前台算价是**设计如此**——洋拍板他俩权限不动，所以前台算价路由登记为 `price:apply 或 price:manage 任一即放行`，杨/韩凭原有 `price:manage` 通过。不是改错了。
1. **扫描清零硬证据**：重跑与 A4 同一逻辑的扫描脚本（app.py + auth/*.py + flask 引用文件，递归展开 target/参数），**附输出：命中数 9 → 0**。⚠️ 只附 `git grep "for g in"` 不够——它只能证明循环形态没了，证明不了赋值/参数形态（本次 1085/1087 的赋值形态、auth/ 三处参数形态都是 grep `for g in` 漏掉的）。
2. `tests/test_no_shadow.py` 新增并全量测试通过（pytest 输出重定向到 %TEMP%，别写仓库内）
3. 权限矩阵自测（测试环境开闸）：
   - 冯茜：POST /api/price/batch 放行、GET /api/admin/price 403、生成托书 200
   - 杨雅雯：POST /api/price/batch 放行（回归）、GET /api/admin/price 放行（回归）
   - 韩文豪：同杨雅雯
   - 游客：批量算价按钮不可见（前端）、直接打接口 403
4. 三个账号（毛骁洋/杨雅雯/韩文豪）生成托书 200 且审计日志署名正确（小叽已在 F:\YXO_TEST 预验过同款改法，最终以合入分支后的代码为准再跑一遍）
