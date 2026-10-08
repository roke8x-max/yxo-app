# 留言 · 给小叽（服务器侧前置核查）

**来自**：芙蕾雅 ｜ **日期**：2026-09-29 ｜ **事项**：yxo-app 权限模块（RBAC）加固 · 生产前置检查

## 背景（自包含，无需翻聊天记录）

洋的团队给 `yxo-app`（渝新欧订舱系统，生产跑在服务器 `10.0.199.184` 的 `127.0.0.1:5011`）新做了一套**登录鉴权 + 权限隔离**模块。其中 `/api/stamp` 这个接口是给**本机 DSK/ATB 邮件转发机器人**用的——这两个机器人在同机以 `http://127.0.0.1:5011/api/stamp` 回环调用，带 `X-Stamp-Token` 校验，写 `records` 表的 `dsk`/`ATB` 时间戳。

现在要加固这个接口（白名单只放 `127.0.0.1` + 拒绝代理头 `X-Forwarded-For`/`X-Real-IP` + 强随机 token 用 `compare_digest` 比较）。但加固前需要你在服务器上确认一件事：**nginx 有没有把这个接口暴露到公网**。如果已经暴露，白名单会被绕过（经 nginx 进来的请求 `remote_addr` 也是 `127.0.0.1`），得先在 nginx 侧摘掉再部署。

## 需要你执行的检查（在服务器 10.0.199.184 上）

**① nginx 是否暴露 `/api/stamp`**（最关键，决定 B 部署前是否要先动 nginx）
在跑 nginx 的机器 / 配置目录：
```powershell
grep -R "stamp" <nginx 配置目录>
# 看有无 location 块把请求 proxy_pass 到 127.0.0.1:5011
```
- 若**有**暴露 → B 部署前必须先摘掉该 `location`（否则白名单形同虚设）。
- 若**没有** → 白名单方案直接生效，无需改 nginx。

**② 旧 DSK/ATB 机器人是否还在跑**（绞杀式重构进度留证）
```powershell
Get-Process -Name python* | Select-Object Id,CommandLine
Get-ScheduledTask | Where-Object { $_.TaskName -match 'mailbot|dsk|atb' }
```
- 确认 `Dsk_Robot` / `Atb_Robot` 是否仍在进程 / 计划任务里。（洋已知它们还在跑，这步用于留证 + 确认重构未完成。）

**③ `/api/stamp` 真实调用来源 IP**（验证确实全是本机）
```powershell
Select-String "api/stamp" <yxo_app 访问日志路径> | ForEach-Object { ($_ -split ' ')[0] } | Sort-Object -Unique
```
- 预期：全部 `127.0.0.1`。若出现外网 IP，说明已暴露，需回头做 ①。

## 结果回传
把以上三条的命令输出贴回给我（或洋）即可。我据 ① 的结果定最终部署动作：
- nginx 已暴露 → 先摘 `location` 再部署 B；
- 未暴露 → 直接部署 B（白名单 + 拒代理头）。

## 范围备注（避免误做）
- 本次只涉及 **B（stamp 加固）的前置核查**，不涉及 A（`AUTH_ENABLED` 灰度开关）。
- 本留言**不涉及**"通知 4 位同事新密码""设 `YXO_AUTH_*` 环境变量""删本机 dev `auth.db`"——那些是 `AUTH_ENABLED` 翻 `=1`（灰度结束切生产）时才做的动作。
- 我没有小叽的直接通道，此留言由洋转发。

—— 芙蕾雅
