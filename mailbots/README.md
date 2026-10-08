# mailbots —— 旧邮件机器人（**生产仍在运行，只修 bug**）

> ⚠️ **本目录是旧系统。新功能一律写 `mailbots_next/`，绝不写在这里。**
> 保留它的原因：**生产此刻跑的就是它**（新版尚未切换）。

---

## 1. 它是什么

5 个独立的邮件机器人脚本，各自监听邮箱、各自转发：

| 脚本 | 负责 |
|---|---|
| `Draft_Forward_Robot.py` | 草单 |
| `Waybill_Robot.py` | 运单号 |
| `Dsk_Robot.py` | DSK |
| `Atb_Robot.py` | ATB |
| `Tracing_Robot_IMAP.py` | 运踪 |

外加 `core/` 公共层、`draft_pending.py`（待确认队列）、`daily_forward_report.py`（每日汇总）、`forward_log.py`（转发留痕）。

---

## 2. 与新系统的关系

| | 旧 `mailbots/` | 新 `mailbots_next/` |
|---|---|---|
| 结构 | 5 个脚本，各有各的 `resolve_recipients` / 去重 | **一条共享流水线** + 6 个「取键插件」 |
| 依赖 | 部分仍读飞书（已弃用） | **已切断**对旧包的反向依赖 |
| 测试 | `tests/unit` 下有 core 层用例 | 22 个测试文件（**不依赖真实邮箱**） |
| 生产 | **在跑** | **未上线** |

**切换前置条件**（缺一不可，详见 `mailbots_next/README.md` 与 `AGENTS.md`）：

1. **必须先停旧、再起新**（否则漏抓或重复转发）。
2. `FORWARD_SINCE` 必须设置 —— 否则首跑 / `UIDVALIDITY` 变化后的全量重扫，**会把历史邮件批量转发给客户**。（新系统已用代码强制，`serve.py` 里有护栏。）
3. `YXO_DB_PATH` 必须显式指向**本地盘** —— 默认走 UNC/SMB，历史上报过 `attempt to write a readonly database`。
4. 企业邮箱文件夹必须先由人工合并（「运单号」+「运单草单」→「草单运单号」），否则轮询抓空。

---

## 3. 生产怎么跑的

| 项 | 值 |
|---|---|
| 运行目录 | `D:\YXO_DATA\yxo_app\mailbots` |
| 服务 | nssm 服务 `YXO-MailBot` + 任务计划 `YXO-Tracing` / `YXO-Dsk` / `YXO-Atb` |
| 日志 | `D:\YXO_DATA\logs\service_out.log` |
| 本次部署/回滚 | `scripts\deploy\Deploy-YXO-MailBots.ps1 -Action update` / `Rollback-YXO-MailBots.ps1` |
| 部署手册 | `scripts/deploy/DEPLOYMENT_MANUAL.md` |

---

## 4. 铁律

- ❌ **绝不在这里实现新功能**（契约铁律）。它只修 bug，保留到旧系统退役。
- ❌ 不要用这里的代码当作"运行系统行为"的唯一依据 —— 生产实际跑的是服务器上那一份。
- ⚠️ 改动前先确认：这条改动**是否也可能需要同步到 `mailbots_next/`**。

---

## 5. 已知坑（本模块相关）

| 坑 | 说明 |
|---|---|
| `Waybill_Robot.py` 是**半截子废弃重构** | 未接入调度，`live=False` 默认。**不能当作运行系统依据** |
| 明文密码历史 | 运行目录曾含明文邮箱密码；仓库版已外置凭据。**不要把运行目录原样拷回仓库** |
| 转发日志是 best-effort | `forward_log.py` 明写"记日志失败绝不能影响转发主流程…本模块内部也全程吞异常" ⇒ **它自己也可能漏记，不能当 100% 转发真值** |
| 查 `forward_log` 的方式 | `forward_log.py` 的 `_conn()` 会 `executescript(DDL)`（可能建表）⇒ **必须用 sqlite 只读打开或复制副本，禁止 `import` 该模块查库** |
| `IMAP SEARCH SINCE` 格式 | 只接受 `DD-MMM-YYYY`（如 `08-Sep-2026`）；传 ISO 日期会**静默失效** |
| `xlrd` 2.x 不支持 `.xlsx` | 必须 openpyxl 回退（openpyxl → xlrd → 文本/CSV） |

---

## 6. 测试

```powershell
& $PY -m pytest -q -p no:cacheprovider mailbots/tests/unit
```

- `tests/testset/`：合成夹具（编号命名，如 `A01_*`、`W03_*`）+ 拆分测试矩阵说明。
- `tests/fixtures/real_imap_samples/`：**真实生产邮件样本**（未脱敏，含真实姓名/邮箱/箱号/订单号与 PDF 附件）。
  ⚠️ 该目录**只有 1 个旧测试文件引用**（`test_planb_forward_builder.py`）。毛骁洋已决定处理这批样本——**处理之前，先把它当新版切换验收的语料用掉**（见 `docs/2026-10-06-邮件转发与主站在线表-现状与建议(给芙蕾雅).md`）。
