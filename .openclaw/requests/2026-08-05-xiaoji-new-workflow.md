# 给小叽：你的工作方式从今天起变了

小叽，之前你在服务器上直接改 `D:\YXO_DATA\yxo_app` 里的代码，改完有时会直接重启服务让改动生效。现在这个方式要改，原因和做法如下。

## 为什么要改

- `D:\YXO_DATA\yxo_app` 是**生产环境**，正在跑真实客户的运踪 / 托书 / 舱单业务。直接改有风险，改错了影响线上。
- 现在**本机也有人在开发**（毛骁洋 + 芙蕾雅，在 `C:\Users\Roke8x\Projects\yxo-app`）。如果服务器和本机各改各的、又不经过 GitHub，两边代码会对不上，合并时必冲突。
- **GitHub 是唯一的代码真相源**——所有改动都要回到 GitHub，大家才能协作。

## 新的工作方式（请严格遵守）

1. 以后你的**开发目录是 `E:\yxo_app_dev`**（不是 D 盘的 yxo_app）。
2. 在 `E:\yxo_app_dev` 里改代码。
3. 改完运行：
   ```powershell
   cd E:\yxo_app_dev
   git add .
   git commit -m "你改了什么（用业务语言写，比如：修复运踪配置表莫斯科站点匹配）"
   git push origin dev
   ```
4. **改完不要去重启生产服务**，也不要碰 `D:\YXO_DATA\yxo_app`。
5. 等毛骁洋 review 后合并到 `main`，再由部署流程（`git pull` + `nssm restart`）更新生产。

## 关于外部模块（D 盘路径）

`admin_api.py` 里加载的 `D:\YXO_DATA\WeComBot\config.py` 和 `D:\YXO_DATA\MailBots\common_io.py`，本机 dev 已改成"缺失时自动虚拟模块降级"。`E:\yxo_app_dev` 同样适用——缺这两个文件不会崩，飞书类接口在本机/开发环境本就不调用，生产环境有真文件则正常。

## 如果你已经在 D 盘改了一半

- 别慌，改动不会丢。告诉毛骁洋或芙蕾雅，我们会先把 D 盘的改动收编到 `dev` 分支。
- 之后统一在 `E:\yxo_app_dev` 开发，D 盘只做部署。

## 总结一句话

**E 盘写代码、push 到 dev、等 review，别碰 D 盘生产。** 有疑问随时问毛骁洋。
