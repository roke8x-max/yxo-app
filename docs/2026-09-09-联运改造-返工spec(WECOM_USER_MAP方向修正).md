# 联运改造返工 Spec（验收发现，交 OpenCode 落码）

> 日期：2026-09-09 ｜ 提出：芙蕾雅（验收）｜ 执行：OpenCode ｜ 状态：待落码
> 背景：联运改造已落码并通过功能验收（134 passed 全绿，4 项行为验证全过）。**功能无需返工**，本 spec 只处理验收发现的「方向性错误 + 2 项可选加固」。
> 前置：本 spec 只动 3 个文件，其余一律不动。

---

## 0. OpenCode 四铁律（洋定）
1. **不瞎改已做好内容** —— 只动本 spec 列出的 3 个文件，禁止"顺手重构"。
2. **不自己 commit / push** —— 改完本地自测，等洋 review + 合并。
3. **做完先自测** —— `pytest mailbots_next/tests/ -q` 全绿。
4. **如实报告，不替拍板** —— 遇到 spec 没覆盖的先问。

---

## 1. 【必修】根 `config.py` 的 `WECOM_USER_MAP` 方向被改反了

### 1.1 现状（错的）

`config.py:140-146` 当前是：

```python
# 企微用户映射：真实姓名 -> 企微 UserID        ← ① 注释也是错的
WECOM_USER_MAP = {
    "毛骁洋": "MaoXiaoYang",                    ← ② 数据方向反了
    "杨雅雯": "wulala",
    "冯茜": "BanXian",
    "韩文豪": "HanWenHao",
}
```

### 1.2 为什么是错的（用法决定方向，不是注释决定）

这张表有两个消费方，**都要求「键 = 企微 UserID，值 = 真名」**：

| 用法 | 代码 | 要求 |
|---|---|---|
| 按真名反查 uid | `wecombot/cs_bot/wecom_api.py:318`<br>`uid = next((k for k,v in WECOM_USER_MAP.items() if v == real_name), None)` | **值**必须是真名 |
| 按 uid 取真名 | `wecombot/server.py:80`<br>`name = WECOM_USER_MAP.get(raw_id)` | **键**必须是 UserID |

按现在的写法（键=真名、值=UserID），两种用法**全部失效**。

### 1.3 根因（重要，别再被带偏）

`config.py:140` 那条注释「真实姓名 -> 企微 UserID」**从一开始就是错的**（历史上数据方向与注释相反），这次改数据的人照着注释"对齐"，于是把数据改成了注释描述的样子。**必须连注释一起改**，否则下次还会有人照注释改回去。

对照 `wecombot/config.py:83` —— 那边的注释是对的，照抄它的写法：

```python
# WeCom UserID -> real name
WECOM_USER_MAP = {
    "MaoXiaoYang": "毛骁洋",
    "wulala": "杨雅雯",
    "BanXian": "冯茜",
    "HanWenHao": "韩文豪",
}
```

### 1.4 改法

把 `config.py:140-146` 整段替换为（**与 `wecombot/config.py:83-89` 完全一致**）：

```python
# WeCom UserID -> real name（注意方向：键=UserID，值=真名；勿照旧注释反转）
WECOM_USER_MAP = {
    "MaoXiaoYang": "毛骁洋",
    "wulala": "杨雅雯",
    "BanXian": "冯茜",
    "HanWenHao": "韩文豪",
}
```

⚠️ 只改这一处。**不要**去动 `wecombot/config.py:83-89`（它本来就是对的）。

### 1.5 影响面说明（避免误判严重性）

这**不影响联运功能**——已实跑确认：新系统 `notify.py:62` 导入 `wecombot.cs_bot.wecom_api` 时，该模块 `:12` 会 `sys.path.insert(0, wecombot目录)`，故 `from config import` 解析到 `wecombot/config.py`，根 `config.py` 的这份**不会被读取**。本次属于**消除地雷**，不是修 bug。

---

## 2. 【可选·建议做】锁住联运行为的显式测试

现状：联运只作为字典数据加进了 `test_core.py:58` 和 `test_p1_coverage.py:47`，**没有任何断言直接验证"联运"本身**。后果：将来有人误删字典里的联运项，测试不会红（其他用例不依赖它）。

建议在 `mailbots_next/tests/test_core.py` 补一个测试（位置：紧邻 `test_get_responsible_person` 之后）：

```python
def test_lianyun_routing():
    """联运（2026-09-09 新增，负责人冯茜）必须能路由到负责同事与收件人。"""
    seed_owner_mapping()
    person = get_responsible_person("联运")
    assert person == "fengqian@cqtransit.com"

    seed_company_recipients()
    to, cc = get_recipients("联运")
    assert "lx_to@test.com" in to
    assert cc == ["3841559246@qq.com"]
```

- 需要的 import（`get_responsible_person` / `get_recipients` / `seed_company_recipients`）该文件内应已存在，**不要新增重复 import**。
- 若 `seed_company_recipients` 不在同一测试类的作用域，按其现有调用方式照抄即可。

---

## 3. 【可选·洁癖】`_SEVEN_COMPANIES` 改名

`mailbots_next/tests/test_p1_coverage.py:39` 的字典现在装了 **8 家**公司，名字还叫 `_SEVEN_COMPANIES`。纯命名问题，不影响功能。

改法（只有 2 处引用，已核实）：
- `:39` `_SEVEN_COMPANIES = {` → `_COMPANY_RECIPIENTS = {`
- `:54` `for company, (to, cc) in _SEVEN_COMPANIES.items():` → `_COMPANY_RECIPIENTS.items()`

⚠️ 全仓库仅这 2 处引用（已 grep 确认），改完跑测试验证无 NameError。

---

## 4. 禁止改动（铁律①）

- ❌ 不动 `wecombot/config.py` 的 `WECOM_USER_MAP`（已正确）。
- ❌ 不动 `mailbots_next/core/store.py`、`FIELD_DEFS`、`USER_COMPANIES`、`AGENTS.md`、`README.md`（已验收通过）。
- ❌ 不动 `mailbots/` 旧系统任何文件。
- ❌ 不顺手重构 `wecom_api.py` 的 `sys.path.insert` 魔法（已列入方案B 重构范围，本次不做）。

---

## 5. 验收标准（我会独立复跑，不采信报告数字）

1. **单测**：`pytest mailbots_next/tests/ -q` 全绿。
   - 若做了 §2，passed 数应为 **135**（基线 134 + 1）。
   - 若只做 §1/§3，passed 仍应为 **134**（不得下降）。
2. **方向自检**（关键）：
   ```python
   import sys; sys.path.insert(0, r"C:\Users\Roke8x\Projects\yxo-app")
   import config as c
   assert c.WECOM_USER_MAP == {
       "MaoXiaoYang": "毛骁洋", "wulala": "杨雅雯",
       "BanXian": "冯茜", "HanWenHao": "韩文豪"}, c.WECOM_USER_MAP
   # 两种用法都要能用
   assert c.WECOM_USER_MAP.get("BanXian") == "冯茜"                       # 按 uid 取真名
   assert next(k for k, v in c.WECOM_USER_MAP.items() if v == "冯茜") == "BanXian"  # 按真名反查 uid
   print("WECOM_USER_MAP 方向正确")
   ```
3. **对照检查**：根 `config.py` 的 `WECOM_USER_MAP` 与 `wecombot/config.py` 的**键和值必须完全一致**。
4. **命中检查**：`config.py` 中不应再出现 `"毛骁洋": "MaoXiaoYang"` 这种「真名: UserID」写法。

---

## 6. 执行顺序

1. 改 §1（必修，含注释）→ 跑 §5.2 方向自检。
2. 改 §2（可选，建议做）→ 跑 pytest，确认 135。
3. 改 §3（可选）→ 跑 pytest 确认无 NameError。
4. 不自 commit，报告实际改动内容与 pytest 数字。
