# yxo-app · OpenClaw 操作手册

> 这是给 AI agent 看的"项目使用说明"。每次 OpenClaw 处理这个项目时，会先读这个文件。

## 这是什么
- 渝新欧铁路货运代理业务系统
- 订舱 / 运踪 / DSK / ATB 邮件协同 + 企微机器人
- Python 脚本 + SQLite（yxo.db）+ NAS 工具链

## 关键路径
- 业务数据：`data/yxo.db`（**gitignore 保护，绝对不要 commit**）
- 价格表：`data/price_config.json`
- 配置：`config_local.py`（**gitignore，不入库**）
- 日志：`logs/*.log`（gitignore）

## 技术栈
- Python 3.11+（不用 3.12 的新语法，保持兼容性）
- SQLite 3
- requests / 企业微信 webhook

## 业务线（分工）
- 太平洋/港九港铁：冯茜
- 同程配/东盟：杨雅雯
- 保时达：韩文豪
- 沙坪坝/中欧木业：内部

## 硬规则（违反任何一条立即停止）
1. **绝不**修改 `data/yxo.db`（生产数据，由人手动管）
2. **绝不**修改 `config_local.py`（含企业微信 webhook URL、邮箱密码等敏感信息）
3. **绝不**把含 `客户名 / 收发货人 / 运价 / 提单号` 的真实数据写入 patch 或测试用例——必须脱敏
4. **绝不**在服务器生产目录 `D:\YXO_DATA\yxo_app` 改代码——那里只跑业务，开发一律在 `E:\yxo_app_dev`
5. **绝不**直接 push 到 `main`——`main` 只接受来自 `dev` 的 Pull Request（本地 pre-push hook 会拦截）
6. **绝不**用 filebrowser / 远程桌面拖拽 / 手动复制的方式往服务器传代码——只能走 git
7. **绝不**改 `requirements.txt` 里的核心依赖版本（requests、flask、sqlalchemy 等），除非用户明确同意

## 推荐工作流
- **分支策略：`dev` 开发 → Pull Request → `main` 生产**（完整说明见 WORKFLOW.md 第三章）
  - `dev` = 集成沙箱，允许有 bug，所有人往这里推
  - `main` = 生产真相，永远保持可运行，只能通过 PR 合入
- 开工前先 `git checkout dev && git pull origin dev`，别基于旧代码开发
- commit 勤一点（存档，只在本机）、push 慎一点（发布，自测通过后再推）
- 推之前必须本地跑一遍受影响的功能，不要把没验证的代码推上 dev
- 申请上生产：`gh pr create --base main --head dev --fill`，等骁洋审核合并
- 生产部署只能用 `scripts\deploy.ps1`（自动备份），出事用 `scripts\rollback.ps1`
- 改完输出 diff 摘要给用户 review，用**业务语言**说清改了什么效果
- 完整流程、部署方式、环境配置、故障自查 → 一律以 **WORKFLOW.md 为准**

## 沟通风格
- 简体中文回复
- 给 patch 前先说"我会改哪几个文件、为什么这么改"
- 改完给一句"如何测试 / 怎么验"
- 不确定时**先问再做**，不要猜
