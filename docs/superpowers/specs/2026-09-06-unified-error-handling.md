# 统一错误处理与响应格式（Spec）

> 日期：2026-09-06
> 状态：已批准（Phase 0 代码已实现并通过冒烟测试，但**尚未提交 git**；Phase 1/2 随 5.1 分层重构推进）
> 上游文档：产品文档 5.2「统一错误处理与响应格式」
> 代码基准：yxo-app（app.py + admin_api.py，共用一个 Flask app，54 路由）
> 关联：docs/superpowers/specs/2026-08-26-mailbots-refactor-design.md（本项目 spec 格式范本）
> 本文取代该产品条目的模糊描述；未被本 spec 覆盖处继续有效。

---

## 0. 本轮新拍板决策（基于 5.2 原始条目 + 5 轮问答）

| # | 决策 | 内容 |
|---|---|---|
| D1 | 覆盖边界 | 统一规范只强制**主 app**（app.py + admin_api.py 蓝图，54 路由）的 AppError + {ok,error,msg}。wecombot 算"隔离治理"（它自己 app 内加纯文本错误处理器防堆栈泄露，不套 JSON 信封）；mailbots_next 彻底排除（保留 error_queue）。 |
| D2 | 错误码形态 | 语义化稳定枚举，SCREAMING_SNAKE_CASE（如 ORDER_NOT_FOUND、INSUFFICIENT_BALANCE、DB_TIMEOUT）。**与 HTTP 状态码完全解耦**。 |
| D3 | 错误码归属 | 不维护全局统一编号表。AppError 基类仅预设 INTERNAL_SERVER_ERROR 默认值；各蓝图模块（routes/orders.py 等）在自己的文件顶部独立定义本模块专属错误码。 |
| D4 | AppError 默认状态 | error_code 默认 "INTERNAL_SERVER_ERROR"；http_status 为显式参数，默认 200。只有"非 AppError 的未知 Exception"被全局兜底捕获时才 → 500。 |
| D5 | 三处理器职责 | ① errorhandler(AppError) → 固定 200，体 {ok:false,error:"CODE",msg}；② errorhandler(HTTPException) → 保留原生 4xx 状态，error 码统一 "HTTP_{code}"；③ errorhandler(Exception) → 固定 500，体 {ok:false,error:"INTERNAL_SERVER_ERROR",msg:"服务异常"}。注册顺序 AppError→HTTPException→Exception。 |
| D6 | 日志策略 | HTTPException→WARNING 记 path/method/IP/code/desc，不记堆栈；AppError→INFO 记 path/method/IP/error_code/msg，不记堆栈；未知 Exception→ERROR 且必须告警，记完整 traceback.format_exc()+类型+str(e)。堆栈落盘是红线。 |
| D7 | 全链路 request_id | before_request 生成 uuid.uuid4().hex[:8] 存 g；所有日志带 [req_id:...]；响应体必须回传 req_id 给调用方。 |
| D8 | 日志轮转 | RotatingFileHandler / TimedRotatingFileHandler 防打满磁盘。 |
| D9 | 收敛策略（三阶段） | Phase0 只加不删（全局网 + request_id + 日志），不碰存量 try/except；Phase1 随蓝图拆分，每迁一个路由原子化改造（删 try/except + 改 raise AppError + 定义本模块码）；Phase2 全量迁移后清零存量 {ok:False} 旧响应。 |
| D10 | 存量状态码迁移规则 | 业务规则违反/资源不存在/参数格式错 → AppError(http_status=200)+业务码；绝不用 HTTP_404 表示业务不存在；业务层参数校验失败不交给 HTTPException。 |
| D11 | R1 错误码下沉 services/（采纳 deepseek） | 错误码在**业务规则所在层** `raise`：服务层（`services/`）或路由层（`routes/`）直接 `raise AppError(CODE, msg)`，而非在路由里临时拼字符串。基类 `AppError` 仅预设 `INTERNAL_SERVER_ERROR` 兜底值；各模块专属码在所属文件顶部常量定义（如 `services/orders.py` 顶部 `ORDER_NOT_FOUND = "ORDER_NOT_FOUND"`）。 |
| D12 | R2 repo 层错误语义化（采纳 deepseek） | 仓储/数据库层（`repositories/db.py` 及其调用）的裸异常（sqlite3.Error / 连接超时 / 约束冲突）**不允许漏到路由**；在 repo 层或 service 层边界映射为语义化 `AppError`（如 `DB_TIMEOUT` / `DB_CONSTRAINT` / `DB_CONNECTION`）。`errors.py` 暂不内置这些码（遵循 D3 各模块自管），但 Phase 1 迁移 repo 层时**必须**做此映射，禁止裸 `sqlite3.Error` 跨越分层。 |
| D13 | R3 内容协商（采纳 deepseek，已落地） | 响应形态按**请求特征**协商：`request.path.startswith("/api/")` **或** `Accept` 含 `application/json` → 返回 JSON 信封；其余（浏览器直接访问 HTML 页面）→ 返回简单 HTML 错误页（`<h1>{code}</h1><p>{msg}</p>`），避免 JSON 500 倒退成浏览器白页。三处理器均按此分支。 |

## 1. 字段命名与响应信封约定

统一错误响应信封（JSON）：

```json
{
  "ok": false,
  "error": "ORDER_NOT_FOUND",
  "msg": "订单不存在",
  "req_id": "a1b2c3d4"
}
```

- `ok`：布尔，错误恒为 false（Python 侧用 `False`，JSON 序列化为 `false`）。
- `error`：SCREAMING_SNAKE_CASE 错误码。业务码由各模块定义；框架级为 `HTTP_{code}`（如 HTTP_404、HTTP_400、HTTP_405）；兜底为 `INTERNAL_SERVER_ERROR`。
- `msg`：给人看的中文说明，不暴露内部细节。
- `req_id`：本请求全链路 ID，所有 JSON 响应（含成功）均回传，便于调用方报障时精准 grep。

成功响应：保持现有 `{"ok": True, ...}` 形状不变，仅额外注入 `req_id`（通过 `after_request` 统一注入，不改各路由 return 语句）。

**DEBUG 模式**：`app.config["DEBUG"]`（默认 False，生产安全）为 True 时，500 错误响应额外带 `"detail": str(e)`，仅本地开发使用；生产环境严格不返回 detail/trace（见验收标准）。

## 2. 总体架构

```
app.py (主 Flask app)
 ├ before_request            → 生成 request_id 存 g
 ├ errors.py                 → class AppError(Exception)  # error_code / msg / http_status
 ├ 三个全局 errorhandler（注册于 app，自动覆盖蓝图 admin_bp 的 18 路由）
 │    ├ @app.errorhandler(AppError)        → 200 + {ok,error,msg,req_id}
 │    ├ @app.errorhandler(HTTPException)   → 原生 4xx + {ok,error:"HTTP_xxx",msg,req_id}
 │    └ @app.errorhandler(Exception)       → 500 + {ok,error:"INTERNAL_SERVER_ERROR",msg,req_id}
 ├ after_request             → 所有 JSON 响应注入 req_id
 └ 路由 app.py(36) + admin_api.py(18=admin_bp)   // Phase0 存量不动
```

- `errors.py` 位于项目根目录，app.py 与 admin_api.py 均 `from errors import AppError`。
- 蓝图 `admin_bp` 注册进同一 app（app.py:29），故 app 级 errorhandler 天然覆盖其 18 路由，无需在蓝图内重复注册。
- 各模块专属错误码：在 routes/orders.py 等蓝图文件顶部以模块常量定义（如 `ORDER_NOT_FOUND = "ORDER_NOT_FOUND"`），不建全局映射表。
- 错误码在**业务规则所在层 raise**（services/ 或 routes/，见 D11）；repo 层裸异常映射为语义 AppError（见 D12）。
- **内容协商（D13）**：三处理器内部先判 `_is_api_request()`——`/api/*` 或 `Accept: application/json` 走 JSON 信封，否则返回简单 HTML 错误页，避免浏览器白页。

## 3. 核心模块（errors.py）

```python
class AppError(Exception):
    def __init__(self, error_code: str = "INTERNAL_SERVER_ERROR",
                 msg: str = "", http_status: int = 200):
        super().__init__(msg)
        self.error_code = error_code
        self.msg = msg
        self.http_status = http_status
```

全局处理器骨架（伪代码）：

```python
@app.before_request
def _assign_req_id():
    g.req_id = uuid.uuid4().hex[:8]

@app.errorhandler(AppError)
def _on_app_error(e: AppError):
    app.logger.info("[req_id:%s] AppError %s %s %s %s",
        g.req_id, request.path, request.method, request.remote_addr, e.error_code)
    return jsonify(ok=False, error=e.error_code, msg=e.msg, req_id=g.req_id), 200

@app.errorhandler(HTTPException)
def _on_http_error(e: HTTPException):
    app.logger.warning("[req_id:%s] HTTP %s %s %s %s %s",
        g.req_id, e.code, request.path, request.method, request.remote_addr, e.description)
    return jsonify(ok=False, error=f"HTTP_{e.code}", msg=e.description or "请求错误",
                   req_id=g.req_id), e.code

@app.errorhandler(Exception)
def _on_unexpected(e: Exception):
    app.logger.error("[req_id:%s] 500 %s %s %s\n%s",
        g.req_id, request.path, request.method, request.remote_addr, traceback.format_exc())
    body = {"ok": False, "error": "INTERNAL_SERVER_ERROR", "msg": "服务异常", "req_id": g.req_id}
    if app.config.get("DEBUG"):
        body["detail"] = str(e)
    return jsonify(body), 500
```

（实际实现含日志轮转 Handler 配置、after_request 注入 req_id、DEBUG 门控，详见 §7。）

## 4. 实施顺序（三阶段，每阶段独立可回滚）

| 阶段 | 内容 | 不变量 / 红线 |
|---|---|---|
| **Phase 0（代码已实现，待提交）** | 新增 errors.py(AppError) + 三个全局 errorhandler + before_request(req_id) + after_request 注入 req_id + 日志落盘（含 RotatingFileHandler 轮转）+ 内容协商 `_is_api_request()`。 | **只加不删**：不改任何现有路由的 try/except，不改现有 `return {ok:False,...}` 格式。目标：19 个裸路由获得兜底（告别 HTML 500）。6 项冒烟测试全 PASS，**代码尚未提交 git**（提交日期待定，届时可回填）。 |
| **Phase 1（随 5.1 蓝图拆分）** | 每将一个路由从 app.py/admin_api.py 迁入 routes/xxx.py，原子化改造：删 try/except + 改 `raise AppError(error_code, msg, http_status=200)` + 定义本模块错误码枚举。 | 业务错→200；资源不存在用业务码非 HTTP_404；参数校验失败不交给 HTTPException。每迁一处少一处旧格式。 |
| **Phase 2（收尾）** | 54 路由全迁后，全项目搜 `{ok:False` + try/except 残留，确认 0 残留，清冗余 import。 | 旧代码彻底清零，项目完全统一。 |

Phase 0 交付后，那 17+30 个存量内联 try/except 仍按旧格式返回（缺 error 码、状态可能 400/403/404/500），全局网只兜它们"未捕获"的异常与框架 4xx；Phase 1 迁移时再逐步补齐 error 码。

## 5. 测试与验收

- 单测（pytest，复用 yxo_test.db 或内存 app）：
  - AppError 被 errorhandler 捕获 → 200 + 正确 error 码 + req_id 回传。
  - `abort(404)` / 路由不存在 → HTTP 404 + error=HTTP_404（**断言状态非 500**）。
  - 路由内主动 raise 非 AppError 异常 → 500 + INTERNAL_SERVER_ERROR；断言响应体**不含** str(e)/traceback（生产 DEBUG 关）。
  - DEBUG=True 时 500 响应含 `detail=str(e)`。
  - 全部 JSON 响应（成功/失败）含 req_id。
  - 日志断言：HTTPException→WARNING 无 traceback；AppError→INFO 无 traceback；未知 Exception→ERROR 含 `traceback.format_exc()`。
- 验收标准：
  1. 19 个裸路由抛异常 → JSON 500 信封，不再是 HTML 页面；
  2. 业务错误（如订单不存在）→ HTTP 200 + error 码，运维 5xx 监控不因之误报；
  3. 路由 404 → HTTP 404（非 500），与业务不存在靠 error 码区分；
  4. 500 日志带完整堆栈 + req_id，可 grep 定位；
  5. 前端改读 JSON 的 ok/error 判断成败，不再依赖 HTTP 状态（breaking change，需前端/wecombot 同步）。

## 6. 部署执行模型

- Phase 0 改动小、低风险，可在主分支直接提交（仍走"先方案后执行"）；Phase 1/2 随蓝图拆分分支进行。
- 运行模型同现状：生产直跑 git 检出目录 `D:\YXO_DATA\yxo_app`。

## 7. 可观测性（日志落盘细则）

1. Handler：RotatingFileHandler（按大小，如 10MB×5）或 TimedRotatingFileHandler（按天），写入 app.log；500 另写 error.log 便于运维单独监控。
2. 所有日志带 `[req_id:...]` 前缀。
3. 500 必须 `traceback.format_exc()` 落盘，禁止只记 str(e)。
4. 4xx / AppError 不记堆栈（防日志膨胀、防干扰运维）。
5. 敏感数据：msg 不拼接 str(e)/SQL；生产响应体不含堆栈。

## 8. 明确不做（YAGNI / 边界）

- 不纳入 wecombot（独立 app，仅做"隔离治理"：纯文本错误处理器防堆栈泄露，不套 JSON 信封）——另行小修，不在本 spec。
- 不纳入 mailbots_next（非 Flask，保留 error_queue）。
- 不维护全局统一错误码编号映射表（各模块自管）。
- 不强制 4xx 改 200（框架 404/405 保留原生状态；业务错才 200）。
- 不在生产响应暴露堆栈 / str(e)。
- 不在 Phase 0 批量替换存量 try/except。

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 业务错误改 200 是 breaking change，前端/wecombot 若仍读 HTTP 状态会误判 | 验收标准明确：调用方改读 JSON ok/error；上线前与前端/wecombot 对齐；可先灰度 |
| Flask errorhandler(Exception) 吞掉 HTTPException 导致 404→500 | 严格按 AppError→HTTPException→Exception 顺序注册；HTTPException 处理器优先匹配；单测断言 404≠500 |
| Phase 0 漏改导致存量路由仍裸奔 | Phase 0 仅加全局网，19 裸路由已由 Exception 兜底覆盖；存量 try/except 在 Phase 1 迁移时清零 |
| 500 日志黑盒 | ERROR 级强制 traceback.format_exc() 落盘 + req_id；单测断言日志含堆栈 |
| 日志打满磁盘 | Rotating/TimedRotating 轮转 |
| 敏感数据经 msg/str(e) 泄露 | 生产响应体禁含 str(e)/trace；msg 手写不拼异常原文 |

## 10. 实施进度

- [x] Phase 0 全局 errorhandler + AppError + request_id + 日志落盘（代码已实现，6 项冒烟测试全 PASS；**尚未提交 git**）
- [ ] Phase 1 随蓝图拆分收敛存量（含 D11 错误码下沉 services/、D12 repo 层语义映射）
- [ ] Phase 2 清零存量
