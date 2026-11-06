# PR-B 补丁任务书（给 OpenCode）· 恢复入口在"配置读不出"时也能用

> 生成 2026-10-16 ｜ **落码：OpenCode ｜ 复验：芙蕾雅 ｜ merge：毛骁洋**
> 依据：PR-B 验收报告 `docs/mainsite/acceptance/2026-10-16-PR-B验收报告-芙蕾雅.md` 第七节「建议①/②」
> **自包含**：背景、锚点、验收都在本文里。
> ⛔ **不单独提交** —— 补完与 PR-B 改动**一起**走一次提交（仍是 5 个文件）。

---

## 1. 为什么补（背景）

PR-B 的 B① 让"价格配置读不出"时**抛错**；B④ 的恢复端点为了"恢复前先留一份当前配置"，用的是：

```python
_save_price_cfg(_load_price_cfg())   # admin_api.py:1044
```

`_load_price_cfg()` 在文件**缺失 / 损坏 / 无权限**时会抛（**这正是 B① 要求的行为**）⇒ 于是**"越需要恢复的时候，越恢复不了"**。

**实测证据**（芙蕾雅用独立探针在临时目录跑真实请求，未改仓库代码）：

| 场景 | restore 结果 |
|---|---|
| 当前配置**正常** | `200 {'ok': True}`，内容回到备份 ✅ |
| 当前配置**损坏** | `500 {'msg': '价格配置读取失败…'}`，**内容未恢复** ❌ |

这个洞是**本 PR 自己引入的组合**（PR-B 之前没有恢复端点），不是历史遗留。

---

## 2. 改动（`admin_api.py`，锚点 `:1044`）

**① 顶部 import 区加一行**（放 `:23` 的 `from datetime import datetime, timedelta` 附近，与它同组）：
```python
import shutil
```
**② 同时删掉 `admin_api.py:832` 的局部 `import shutil`** —— 那处在 `_save_price_cfg` 里，改成顶部统一后它就成了重复 import（**这是纯风格归一，零行为变化**）。

**③ 把 `api_price_restore()` 里的 `_save_price_cfg(_load_price_cfg())` 换成原始文件拷贝**（绕开解析）：
```python
    # 恢复前先备份当前（留一份）：用原始拷贝、不经解析 ——
    # 当前配置读不出来时（正是要恢复的场景）也必须能备份。
    if os.path.exists(PRICE_CONFIG):
        os.makedirs(PRICE_BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(PRICE_CONFIG, os.path.join(PRICE_BACKUP_DIR, f"price_config_{ts}.json"))
    _prune_price_backups(keep=30)
    atomic_write_json(PRICE_CONFIG, restored)
```

**六条硬要求（逐条都要满足）**：
1. ✅ **`ts` 必须是 `%Y%m%d_%H%M%S`** —— `_prune_price_backups` 与 `api_price_backups` 都只认
   `^price_config_\d{8}_\d{6}\.json$`。**格式写错，这份备份不会被清理、也不会出现在恢复下拉里，变成永久孤儿。**
2. ✅ **存在性判断保留**（`if os.path.exists(PRICE_CONFIG)`）—— 配置缺失时不硬拷贝。
3. ✅ **补 `os.makedirs(PRICE_BACKUP_DIR, exist_ok=True)`**（⚠️ 原实现这一步是在 `_save_price_cfg` 里做的；改成 `copy2` 后不再经过它，**必须自己建目录**，否则目录不存在时 `copy2` 直接抛 `FileNotFoundError` —— 用新洞换旧洞）。
4. ✅ **补完顺手调一次 `_prune_price_backups(keep=30)`** —— 与 `_save_price_cfg` 的保留语义保持一致（这条备份路径不经过 `_save_price_cfg`，不显式调用就没人管它的上限）。
5. ✅ **`datetime` 可用性 —— 已核实现状，无需新增**：`admin_api.py:23` 顶部已有
   `from datetime import datetime, timedelta`，所以示例里的 `datetime.now()` 直接可用，**不会 NameError**。
   （⚠️ 但**落码时若发现顶部没有这一行，必须补上**——本文件另有 20+ 处 `datetime.` 用法依赖它。）
6. ✅ **`import shutil` 必须写在顶部 import 区（与 `:23` 的 `from datetime import datetime, timedelta` 同组）；不得写在任何函数内。**
   同时**必须删掉** `admin_api.py:832`（`_save_price_cfg` 内）那处既有的局部 `import shutil` —— 否则文件里保留两处 `import shutil`。
   （本条是**纯风格归一、零行为变化**，但改动边界严格：**只准增顶部 1 行、删 `:832` 1 行**。）

**不要动**：文件名白名单校验、`src` 存在性 404、备份解析失败 500、审计 `恢复价格`、`atomic_write_json` 写回 —— 这些 PR-B 已验过，保持原样。

> 📌 **`import shutil` 自检口径（改前/改后各 grep 一次，别把"恰好 1 处"当死判据）**
> 1. **落码前**执行 `grep -n "import shutil" admin_api.py`，记下**处数 N 与行号**。
>    - 芙蕾雅已实测：**改前 N = 1**，唯一一处就是 `:832`（在 `_save_price_cfg` 函数体内），配合 `:836` 的 `shutil.copy2` 使用。
>    - ⚠️ **若你实测 N > 1（别处还有局部 import）→ 停下报告，只登记不删**，不要顺手"清理"其它处。
> 2. **落码后**再执行一次，记下处数 **M**。**判据：M == 1，且这一处位于顶部 import 区。**
> 3. ⚠️ **算式提醒**：本次是「**顶部 +1、局部 −1**」，所以 **N − M = 0（1 → 1）**，**不是 1**。
>    （"删掉的重复数"≠ N−M；正确表述是：**删除的行数 == 改前那 N 处局部 import**。）
> 4. 报告里把 **N、M 的处数**与**具体行号**都写出来 —— **M 要写"在哪一行"（应为顶部 import 区那行，约 `:23` 附近），不能只写"M=1"**。
>    目的：改前 `:832` → 改后 `:23` 附近，芙蕾雅收口复核时**一眼锁定**，不必再 grep 一次。

> 📎 **已评估、不修（记录备查，勿顺手改）**
> - **秒级时间戳重名**：同一秒内两次备份会互相覆盖。既有 `_save_price_cfg` 就是这个精度，**本补丁未降低精度**，不算新引入；且两次备份需落在同一秒（单管理员 UI 下几乎不可能）⇒ 不阻塞、不修。
> - **`_prune_price_backups(keep=30)` 在 `if` 之外**：即使没执行 `copy2` 也多调一次 prune —— **幂等**，无副作用，保持现状。

---

## 3. 新增测试（`tests/test_price_admin_hardening.py`）

### ① 新用例：配置损坏/缺失时恢复也能用 —— **必须拆成两个测试函数**
- `test_restore_works_when_config_corrupt`：造一份**合法备份** → 把当前 `PRICE_CONFIG` 写成**非法 JSON** → `POST /api/admin/price/restore` → 断言 **200 且 `ok=True`**，且配置内容 == 备份内容（**回滚成功**）。
- `test_restore_works_when_config_missing`：**同样先造合法备份** → 把当前 `PRICE_CONFIG` **删掉** → 同样断言 **200** 且内容 == 备份内容。

> ⛔ **不得把它们写成一个测试函数里连跑两个场景，也不得共用一个函数内的状态而不重置。**
> 原因：场景 A 跑完后 `PRICE_CONFIG` **已经被恢复成合法内容**了，此时再跑"缺失"场景，当前配置根本不是缺失状态 → 变成在测"**合法配置下的 restore**"，断言照样绿 ⇒ **假绿**。
> 拆成两个函数是充分隔离：`client` 夹具是**函数级**且用 `tmp_path`，每个函数拿到**全新的临时目录**，天然互不污染。

### ② 加严：审计必须落下"旧值→新值"（建议②）
在 `test_price_write_audit_trail` 里**连续改两次汇率**（如先 `7.0` 后 `8.0`），再断言那条 `改价·汇率` 的 `detail` **同时**满足：
- 含 `→` 连接符；
- 含**旧值**与**新值**（如 `"7.0" in dt and "8.0" in dt`）。

`改价·条目` 同理（覆盖已有条目 → detail 里应能看到 `旧 dict→新 dict`）。
> 说明：这是**加严不是放宽**，不改变任何既有断言口径。

---

## 4. 分工与验证顺序（照此执行）

**OpenCode 做**：落码 → `tests/test_price_admin_hardening.py` **单跑绿**（附输出）→ **全量 `0 failed`**（附汇总行）→ 报告（改动 `文件:行号` + 命令 + 输出末几行）。

> ⚠️ **报告里必须单独列明 `admin_api.py:832` 这一处改动**（删掉 `_save_price_cfg` 内那行局部 `import shutil`），写清：**改了哪一行、为什么（与顶部 import 归一并）、并显式声明"零行为变化"**。
> **不要**把它混进"改动清单一笔带过" —— 这是本补丁里**唯一触碰 PR-B 已验代码**的一处，必须可一眼定位。

**芙蕾雅做**（OpenCode 停下后）：
1. **先做"风格归一"硬检查（新增）**：`git diff admin_api.py` 里 **`_save_price_cfg` 函数体应只有"删掉那一行 `import shutil`"这一处改动**。
   - diff 若在该函数里**多出任何一行**，就**不是风格归一，而是偷偷改了行为** —— 这条能一眼定性，发现即停、要求退回。
2. **反向变异**验两条护栏是否真的在测：
   - 把 `shutil.copy2` 那段还原成 `_save_price_cfg(_load_price_cfg())` → 新用例①**必须变红**；
   - 把审计 `detail` 的**旧值去掉** → 审计用例②**必须变红**（PR-B 验收时这一步是全绿，正是它证明了"旧值"当时没护栏）。
3. **正常路径复验**：配置正常时 restore 仍返回 `200` 且内容回滚（不能为了"救损坏场景"把正常路径改坏）；B④ 的备份/保留用例（覆盖 `_save_price_cfg` 正常路径）须仍绿。
4. **`import shutil` 收口复核（对行号，不只看数量）**：`grep -n "import shutil" admin_api.py` 应**恰好 1 处**，且行号落在**顶部 import 区（约 `:23` 附近）**；与 OpenCode 报的 N / M 及其行号对照 —— 预期 **改前 `:832` → 改后 `:23` 附近**。
5. **测试隔离复核**：确认损坏 / 缺失两场景是**两个独立测试函数**（如 `pytest --collect-only -q tests/test_price_admin_hardening.py` 能看到 `..._corrupt` 与 `..._missing` 两条），**不是**一个函数连跑两场。
6. **字节级还原**变异（`cmp` 校验），确认 `git diff` 回到预期。
7. `git add` **精确 5 个文件**（`admin_api.py`、`app.py`、`auth/rbac.py`、`templates/admin.html`、`tests/test_price_admin_hardening.py`）→ commit → push → 开 PR。

**毛骁洋**：网页端 Squash merge。

---

## 5. 共同硬约束（同四件套任务书第 2 节）
- **OpenCode 不做任何 git 写操作**（不 `commit` / 不 `push` / 不开 PR / 不建分支）。
- 报告必须附实跑证据（命令 + 输出末几行 + `文件:行号`）；禁止「应该没问题」。
- **不得为了让测试变绿而放宽任何业务判据**。
- 测试**不得连生产库/生产配置**（沿用现有夹具的 monkeypatch 到 `tmp_path`）。
- 发现计划外问题**只登记，不顺手修**。
- 跑测试：`& $PY -m pytest -q -p no:cacheprovider`（`$PY` = `D:\python.exe`，先自检 `sys.executable`）；输出**不要**重定向到仓库内文件。
