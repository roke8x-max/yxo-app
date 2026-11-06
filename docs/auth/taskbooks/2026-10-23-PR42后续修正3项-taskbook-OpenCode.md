# 补丁任务书：PR#42 后续修正 3 项（OpenCode 执行）

- 日期：2026-10-23
- 来源：`\\10.0.199.184\yxo_data\芙蕾雅_小叽_留言\2026-10-23-小叽工单-PR42后续修正3项.md`（洋实测反馈，小叽整理）
- **代码基线：main `439bcce`**（PR#43 只改了 3 份文档，本任务涉及的行号不受影响）
- 目标分支：从最新 main 拉扁平命名临时分支（如 `fix-admin-modal-and-pw`）
- 优先级：**第 1 项阻塞 pull**（弹窗关不掉）；第 2、3 项同批做

---

## 〇、结论先说

三项我都核过代码，全部成立。**第 1 项是我在 PR#42 验收时漏掉的**——我看了那段 CSS 新增，但没推特异性（`.hidden` 在前、`.modal-mask` 在后，同为单类选择器，后者胜），这类问题读 diff 也能抓到，是我该抓没抓到。本次除了修，还加一条**源码级回归断言**防止再犯。

---

## 一、🔴 后台「重置密码」弹窗常显且关不掉

### 事实（我核过的行号）

```
templates/admin.html:52   .hidden { display:none; }          ← 无 !important
templates/admin.html:56   .modal-mask { ... display:flex; ... }  ← 同特异性、在后面 → 胜出
templates/admin.html:299  <div id="resetModal" class="modal-mask hidden">
```

⇒ `#resetModal` 的 computed `display` 是 **flex**，从进页面就可见；`closeResetPw()` 加 `hidden` 也压不过。
主站不受影响：`static/style.css:203` 是 `.hidden { display: none !important; }`；`templates/admin.html` **没有**引入 `style.css`（只有内联 style）。

### 改法

**⓪ 加 `!important` 之前先做冲突检查（必做，我已核过但你要自己再确认一遍）：**

```bash
grep -n 'class="[^"]*hidden[^"]*"[^>]*style=' templates/admin.html        # 同元素 .hidden + 行内 style
grep -n 'style="[^"]*display[^"]*"[^>]*class="[^"]*hidden' templates/admin.html
```

加 `!important` 后，**作者样式表的 `!important` 会压过普通的行内 `style="display:xxx"`** ——
若某元素同时带 `class="...hidden..."` 和 `style="display:flex"`，它会永远显示不出来。

> ✅ 我已经查过（`0bfae37`）：**两种写法都是 0 命中**，且 `openResetPw()` 显示弹窗用的是
> `document.getElementById("resetModal").classList.remove("hidden")`（`:932`），不是 `style.display = "flex"` ⇒ 可以安全加 `!important`。
> 若你改后发现别处用了 `style.display`，**先停下来报告**，不要自己把那些地方一起改。

**① `templates/admin.html:52`：**

```css
/* ⚠️ 必须带 !important：本页 .modal-mask 在后方定义 display:flex，
   特异性相同会覆盖 display:none，导致被 .hidden 遮住的弹窗默认显示且关不掉。 */
.hidden { display:none !important; }
```

**② 防御：`submitResetPw()` 与 `resetRandomPw()` 开头各加一段**（避免把技术错误抛给用户）：

```js
  if (!_resetUser) {
    const e = document.getElementById("resetErr");
    if (e) e.textContent = "未选中用户：请先关闭本弹窗，在「用户管理」表格里重新点一次「重置密码」按钮。";
    return;
  }
```

> `resetRandomPw()` 里没有 `resetErr` 变量时自己取一次，别直接用未定义变量。

**③ 加分项（建议顺手）：`closeResetPw()` 里把 `_resetUser` 清空** —— 关掉弹窗后状态就失效，语义更干净（`_resetUser = ""; _resetSelf = false;`）。

### 验收

| # | 场景 | 期望 |
|---|---|---|
| 1 | 打开 `/admin` | 看不到弹窗 |
| 2 | 点「用户管理」tab | 列表正常，弹窗仍不可见 |
| 3 | 点某行「重置密码」 | 弹窗出现，标题带用户名 |
| 4 | 点「取消」 | **关闭** |
| 5 | 无选中用户就提交 | 提示「未选中用户…」，**不发请求** |

> 这条**必须真的在浏览器里看一次**（用 DevTools 读 `getComputedStyle(document.getElementById("resetModal")).display`，隐藏时应为 `none`）。
> 我的自动化只到代码层，验不了 computed style。

---

## 二、🟡 密码长度 12 → 8 位

### 改法

**`auth/service.py:68-72`：**

```python
def password_strength(password):
    """新密码强度：长度≥8、含大小写字母、含数字（2026-10-23 毛骁洋要求由 12 位降为 8 位）。"""
    p = password or ""
    if len(p) < 8:
        return False, "新密码长度至少 8 位"
    ...
```

**同步文档（三处，别漏）**：
- `docs/auth/specs/2026-09-21-permission-rbac-module-design.md:297`：「长度≥12」→「长度≥8」
- `docs/auth/specs/2026-10-22-改密界面与后台用户管理-spec.md`：`:63`、`:112`、`:291` 三处「≥12 位 / 至少 12 位」→ 8 位
- `docs/auth/acceptance/2026-10-23-...-验收报告-芙蕾雅.md:36` 属**历史记录，不改**（我会在报告里补一行"后续已改 8 位"）

**测试（`tests/auth_gate_test.py` 追加 2 条边界）**：

| 用例 | 断言 |
|---|---|
| 7 位（`Abc1234`） | 400，msg 含「至少 8 位」 |
| 8 位合规（`Abc12345`） | 200 且能用新密码登录 |

> ⚠️ **改完 `service.py` 先跑一次 `pytest tests/auth_gate_test.py`**，检查现有断言有没有把「12 位」写死。
> 我已 grep 过 `tests/`：**当前没有 `"12 位"` 字面断言**（`test_change_password_flow` 只判 `status_code == 400`），
> 所以理论上不会挂 —— 但**你要自己再确认一遍**（改了 msg 文案却漏看断言，是常见的红法；真红了就改成 8 位或只判状态码）。

**测试（`tests/auth_gate_test.py` 追加 2 条源码级回归锁，防止以后被人删回去）**：

```python
def test_admin_hidden_has_important():
    """admin.html 的 .hidden 必须带 !important，否则 .modal-mask 会覆盖 → 弹窗常显且关不掉。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "templates", "admin.html"), encoding="utf-8").read()
    m = re.search(r"\.hidden\s*\{[^}]*display\s*:\s*none\s*!important", src)
    assert m, "admin.html 的 .hidden 必须带 !important"


def test_password_min_length_rejects_7():
    """7 位必须拒（与 2026-10-23 洋的要求：最少 8 位一致）。"""
    from auth import service
    ok, msg = service.password_strength("Abc1234")      # 7 位
    assert not ok and "8 位" in msg


def test_password_min_length_accepts_8():
    """8 位且含大小写+数字 → 通过。"""
    from auth import service
    ok, _msg = service.password_strength("Abc12345")    # 8 位
    assert ok
```

> 三条都**必须写进测试文件**，不能只在报告里给 `git grep` 结果 —— grep 只能证明"落码时改过"，证明不了"以后没被删"。
> `os` 该文件已 import（`:12`）；**`re` 没有**（本任务书原写"`re` 已 import"，是错的 —— 执行方实测发现并补了 `import re`，2026-10-26 更正）。
>
> **注意切分粒度**：7 位拒、8 位过**分成两个 test 函数**（不要合成一个）——红了能一眼看出是"没拒住 7 位"还是"8 位被误拒"。
> 加上 `test_admin_hidden_has_important`，本次**新增 3 条测试**，全量应为 **616 + 3 = 619**。

### 影响面（确认不会误伤）

- 种子口令（`YXO_AUTH_PASSWORD_*`）**不走** `password_strength`，直接哈希 ⇒ 不受影响
- 已存密码是哈希，规则变化不会让旧密码失效
- 前端没有硬编码 12 位提示（已 grep 确认），提示全来自后端 `msg` ⇒ 前端不用改

---

## 三、🟡 「显示密码」按钮常驻

### 事实

5 个密码框全是**浏览器原生** `<input type="password">`，前端**没有**自定义眼睛按钮（已 grep：`pw-eye`/`ms-reveal` 0 命中）。
洋看到的眼睛是 Edge 的 `::-ms-reveal`（聚焦且有内容才出现，失焦即隐藏），**前端无法让它常驻** ⇒ 必须自己画按钮。

涉及 5 个框：

| 文件 | id |
|---|---|
| `templates/index.html:210` | `chpwOld` |
| `templates/index.html:211` | `chpwNew` |
| `templates/index.html:212` | `chpwNew2` |
| `templates/admin.html:305` | `resetPw` |
| `templates/admin.html:307` | `resetPw2` |

> ⚠️ `loginPass`（主站 `:229`、后台 `:100`）**本次不加**，保持原生行为（工单只列这 5 个）。

### 改法

**HTML（每个框包一层，注意 `type="button"`）：**

```html
<div class="pw-wrap">
  <input id="resetPw" type="password" autocomplete="new-password">
  <button type="button" class="pw-eye" data-target="resetPw"
          title="显示密码" aria-label="显示密码">👁</button>
</div>
```

主站那三个框原来带 `style="width:100%"`，包进 `.pw-wrap` 后由 CSS 的 `.pw-wrap input { width:100% }` 接管，可去掉行内宽度。

**CSS**：
- 主站 → `static/style.css` 追加；**同时把 `templates/index.html:7` 的 `style.css?v=` 换新值**（如 `v=20261023pw`）
- 后台 → `templates/admin.html` 内联 `<style>` 追加（后台不引 style.css）
- **两处都要改，别只改一处**（这 5 个框跨两个页面，CSS 各有一份）
- `box-sizing` 无忧：两页都有全局 `* { box-sizing: border-box; }`（`static/style.css:35`、`admin.html:19`），
  所以 `.pw-wrap input { width:100%; padding-right:38px }` 不会撑出容器；**主站那三个 input 上的行内 `style="width:100%"` 可以删掉**（交给 CSS 管）

```css
.pw-wrap { position: relative; display: block; }
.pw-wrap input { width: 100%; padding-right: 38px; }
.pw-eye {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  background: none; border: none; padding: 4px 6px; cursor: pointer;
  font-size: 15px; line-height: 1; opacity: .55; border-radius: 4px;
}
.pw-eye:hover { opacity: 1; background: rgba(0,0,0,.05); }
.pw-eye.on { opacity: 1; }
input[type="password"]::-ms-reveal,
input[type="password"]::-ms-clear { display: none; }   /* 否则 Edge 会出现两个眼睛 */
```

**JS（两页各绑一次；主站放 `static/app.js` 的 `enterApp()` 里，后台放 `enterAdmin()` 之后或脚本末尾——`admin.html` 的脚本在 body 末尾，元素已存在）**

```js
document.querySelectorAll(".pw-eye").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    const inp = document.getElementById(btn.dataset.target);
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    btn.classList.toggle("on", show);
    btn.title = show ? "隐藏密码" : "显示密码";
    inp.focus();
    try { const n = inp.value.length; inp.setSelectionRange(n, n); } catch (err) {}
  });
});
```

**关闭弹窗必须复位成圆点**（否则明文残留在界面上）：

> 两个关闭函数的**实际名字与位置（已在 `0bfae37` 核过）**：
> - 主站 `static/app.js:89` **`closeChangePw()`**（`openChangePw()` 在 `:77`）
> - 后台 `templates/admin.html:936` **`closeResetPw()`**
>
> 若你改完代码后行号/名字变了，用 `grep -n 'chpwModal' static/app.js`（找 `classList.add("hidden")` 那处）
> 和 `grep -n 'resetModal' templates/admin.html` 定位，**别凭名字猜**。

```js
// closeChangePw()（app.js:89）里
["chpwOld", "chpwNew", "chpwNew2"].forEach((id) => {
  const el = document.getElementById(id); if (el) el.type = "password";
  const b = document.querySelector('.pw-eye[data-target="' + id + '"]');
  if (b) { b.classList.remove("on"); b.title = "显示密码"; }
});
// closeResetPw()（admin.html:936）里同理，换成 resetPw / resetPw2
```

### 验收（核心是第 13 条）

| # | 场景 | 期望 |
|---|---|---|
| 12 | 5 个框聚焦 | 眼睛可见 |
| 13 | **点框外失焦** | **眼睛仍在** ← 洋最在意的一条 |
| 14 | 点眼睛 | 变明文；再点回圆点 |
| 15 | 切换后光标 | 在末尾 |
| 16 | 关弹窗再打开 | 恢复圆点，**不残留明文** |
| 17 | Edge | **只有一个眼睛**（原生已藏） |
| 18 | 点眼睛 | 不会误提交 |

---

## 四、回归与红线

- ❌ **不要 commit / push / 开 PR**，落码后停在工作区等复验
- ❌ 不动后端鉴权、影子期守卫、`ROLE_PERMS`、会话机制
- ✅ 全量测试判据 **`0 failed`**。预期条数：**616 + 3 = 619**（2 条密码边界 + 1 条 CSS 源码锁）
- ✅ 影子期（`AUTH_ENABLED=0`）四个账号接口仍 403；退舱守卫仍生效
- ✅ 改了 `static/app.js`、`static/style.css`、`templates/index.html` ⇒ **`app.js?v=` 与 `style.css?v=` 都要 bump**
- ✅ 加了 `!important` 后，若发现某元素原本靠行内 `style="display:..."` 显示，**停下来报告**（见 §一 ⓪ 的冲突检查）

## 五、交付报告要给的

1. 改动文件与关键行号（`.hidden`、`password_strength`、`.pw-eye` CSS、5 个框的 HTML、JS 绑定、两个 `?v=`）。
2. 全量 pytest 结果（`0 failed`，预期 **619** 条）+ 新增 3 条用例名。
3. `git grep -n "\.hidden" templates/admin.html`（确认带 `!important`）
4. `git grep -n "len(p) < 8" auth/service.py`（确认改了）
5. 一句确认：**关弹窗后密码框已复位为 `type="password"`**（5 个框都要，两页各一次）。
6. 一句说明：`/admin` 首屏弹窗是否可见 —— **能本机用浏览器读 `getComputedStyle(document.getElementById("resetModal")).display` 就给结果；不能就写「未验，需实机」**，**不要虚报**。
7. 一句确认：§一 ⓪ 的两条 grep 冲突检查结果（都应为空）。
