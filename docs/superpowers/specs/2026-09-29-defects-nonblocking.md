# 非阻塞缺陷登记（不阻塞 A+B+v2.5 部署）

> 登记 3 个顺手发现。**均不阻塞本次部署**（A+B+v2.5 同批，AUTH_ENABLED=0）。另行排期修复。
> 注：其中 D1、D3 已见于 PR #25 描述「顺带记录」段；此处独立成册，便于单独排期、不与 PR 混淆。

---

## D1. admin.html / tuoshu.html 缺 `#loginModal`

- **现象**：会话过期后，这两个页面调用 `showLoginModal()` 静默失败——不弹登录层、不跳首页（用户以为页面卡死）。
- **根因**：`static/app.js` 的 `showLoginModal()` 有 `if (!m) return;`（其中 `m = document.getElementById("loginModal")`）。v2.5 在 `index.html` 已加 `#loginModal` 元素，但 `admin.html` / `tuoshu.html` 的模板改动（`+48/−55`、`+34/−42`）**未补该元素**。
- **影响**：仅影响「会话过期后恰停留在 admin/tuoshu 页」的用户体验；不影响数据与安全。
- **建议修法（二选一）**：
  1. 在 `showLoginModal()` 内对 `m` 为 null 加兜底 `location.href = "/"`（跳首页重新登录）；**或**
  2. 给 `admin.html` / `tuoshu.html` 补 `#loginModal` 片段（与 `index.html` 同结构）。
- **状态**：非阻塞，单独排期。

---

## D2. `.gitignore` 裸 `test_*.py` 吞掉 `tests/` 测试文件

- **现象**：`.gitignore` 含裸模式 `test_*.py`，例外只放行 `!mailbots_next/tests/test_*.py` ⇒ `tests/` 下任何 `test_*.py` 会被**静默忽略、不入库**。
- **根因**：模式过宽。本次 `tests/auth_gate_test.py` 因命名是 `*_test.py`（**不匹配** `test_*.py`）**恰好躲过**；但若后续按惯例命名 `tests/test_xxx.py` 会直接丢失、不进版本库。
- **影响**：CI / 干净克隆漏跑测试，掩盖回归（隐蔽性强）。
- **建议修法（二选一）**：
  1. `.gitignore` 改为只忽略特定旧目录：`mailbots/**/test_*.py`、`mailbots_next/**/test_*.py`；**或**
  2. 显式放行：`!tests/**`、`!tests/test_*.py`（放在裸 `test_*.py` 之后）。
- **状态**：非阻塞，但建议尽快修（避免下次测试静默丢失）。

---

## D3. `/api/stamp` 失败响应由 JSON 变 HTML

- **现象**：B 工单把 `/api/stamp` 的拒绝改为 `abort(403)`，Flask 默认返回 **HTML 错误页**；而本系统其余接口拒绝均用 `jsonify` 返回 JSON（`{"ok":False,...}`）。
- **根因**：`app.py` 的 `api_stamp` 使用 `abort(403)`（B 工单 §1.1–1.3），未走 `jsonify`。
- **影响**：调用方为同机 localhost DSK/ATB 机器人（`mailbots/Dsk_Robot.py:730` 直连 `http://127.0.0.1:5011/api/stamp`），**只看状态码** ⇒ 当前无碍；但若将来有调用方解析响应体，会拿到 HTML 而非结构化 JSON。
- **建议修法**：`api_stamp` 的拒绝分支改为 `return jsonify({"ok": False, "error": "HTTP_403", "msg": ...}), 403`（与门禁 `_deny` 一致），保持 API 响应形态统一。
- **状态**：非阻塞（当前调用方不解析 body），建议顺手改一致。

---

## 汇总

| 编号 | 问题 | 阻塞部署？ | 建议修法位置 |
|---|---|---|---|
| D1 | admin/tuoshu 页缺 `#loginModal` | 否 | `static/app.js` 兜底 或 两模板补元素 |
| D2 | `.gitignore` 吞 `tests/test_*.py` | 否（但隐蔽） | `.gitignore` 收窄/放行 |
| D3 | `/api/stamp` 拒返回 HTML | 否 | `app.py` api_stamp 改 `jsonify` |
