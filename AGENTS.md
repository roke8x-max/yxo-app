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
5. **绝不**改 `requirements.txt` 里的核心依赖版本（requests、flask、sqlalchemy 等），除非用户明确同意

## 推荐工作流
- **分支策略：只用 `main` 一条分支**，不开 `feature/*` / `fix/*` / `dev`（完整理由见 WORKFLOW.md 第三章）
- 隔离靠的是「不同机器各有一份仓库」，不是分支：没 push 之前谁也看不见谁
- commit 勤一点（存档，只在本机）、push 慎一点（发布，功能确认后再推）
- 改完输出 diff 摘要给用户 review，用**业务语言**说清改了什么效果
- 完整流程、部署方式、环境配置、故障自查 → 一律以 **WORKFLOW.md 为准**

## 沟通风格
- 简体中文回复
- 给 patch 前先说"我会改哪几个文件、为什么这么改"
- 改完给一句"如何测试 / 怎么验"
- 不确定时**先问再做**，不要猜
