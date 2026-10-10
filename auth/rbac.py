#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RBAC 定义 + 全路由权限登记表（spec §3 / §6 / §7）。

ROUTE_PERMISSIONS: {(method, rule): "public" | "authenticated" | "<perm>"}。
rule 必须用 Flask 的 url_rule.rule 原串（含 <int:rid> 等占位符），
门禁按 request.url_rule.rule 查表。

偏离 spec 说明（落码时按写入表/现存路由定的）：
 1. §7.4 的 `GET /api/meta`（认证身份）与现存表格元数据接口 `/api/meta`
    重名——现存接口是前端启动强依赖（static/app.js:1500），改为
    `GET /api/auth_meta`，现存 `/api/meta` 保持 public。
 2. §7.1 漏登了现存 `GET /api/meta`（表格元数据），此处补登记 public。
  3. `POST /api/stamp`：写入目标表 = records(dsk/ATB)，但认证靠独立的
     X-Stamp-Token（供 DSK/ATB 机器人无 session 调用），门禁层标 public
     + CSRF 豁免，函数内仍校验 token。
     B 加固：函数体白名单 127.0.0.1/::1 + 拒 X-Forwarded-For/X-Real-IP 代理头
     + hmac.compare_digest；config 弱默认已删，生产缺 token 由 init_auth 拒启。
  4. `POST /api/train_status`：写入目标表 = train_meta（内部状态表），
     确认版 2026-10-06 §2.1 已拆为独立 `train:manage`。
  5. `POST /api/import` / `/api/import_upload`：写入目标表 = records
     （import_excel.run_import 直接写库），确认版 2026-10-06 §2.1 已拆为独立 `data:import`。
"""
PUBLIC = "public"
AUTHENTICATED = "authenticated"

PERMISSIONS = {
    "record:read": "订舱主数据读",
    "record:write": "订舱主数据写",
    "record:export": "订舱主数据导出",
    "data:import": "Excel台账批量导入",
    "options:manage": "选项/字段维护",
    "trash:purge": "回收站彻底删除",
    "manifest:import": "舱单空跑/上传",
    "manifest:apply": "舱单应用/回退/还原",
    "tuoshu:generate": "托书生成",
    "price:manage": "价格维护与算价",
    "config:manage": "配置管理",
    "train:manage": "班列状态维护",
    "admin:view": "管理页查看",
    "user:manage": "用户管理",
}

ROLES = {
    "admin": "运维负责人（毛骁洋）",
    "hanwenhao": "韩文豪",
    "yangyawen": "杨雅雯",
    "fengqian": "冯茜",
    "visitor": "外部只读方",
}

# 确认版 2026-10-06 §2：admin 全+user:manage；hanwenhao/yangyawen 按矩阵；
# fengqian 只读为主+record:write+manifest:import+train:manage；visitor 仅 read+export。
ROLE_PERMS = {
    "admin": ["record:read", "record:write", "record:export",
              "data:import", "options:manage", "trash:purge",
              "manifest:import", "manifest:apply", "tuoshu:generate",
              "price:manage", "train:manage", "config:manage",
              "admin:view", "user:manage"],
    "hanwenhao": ["record:read", "record:write", "record:export",
                  "manifest:import", "tuoshu:generate",
                  "price:manage", "train:manage"],
    "yangyawen": ["record:read", "record:write", "record:export",
                  "manifest:import", "tuoshu:generate",
                  "price:manage", "train:manage"],
    "fengqian": ["record:read", "record:write", "record:export",
                 "manifest:import", "train:manage"],
    "visitor": ["record:read", "record:export"],
}


def expand_perms(perms):
    return sorted(set(perms))


def role_permissions(role):
    return expand_perms(ROLE_PERMS.get(role, []))


def has_permission(role, perm):
    return perm in ROLE_PERMS.get(role, [])


# ---------------- 全路由权限登记表（spec §7，含 §7.4 认证路由） ----------------
# 键：(HTTP方法, Flask rule 原串)。GET/POST 同规则需拆行登记。
ROUTE_PERMISSIONS = {
    # 页面 HTML：一律 public，权限在 API 层强制（spec §6.1 页面路由处理）
    ("GET", "/"): PUBLIC,
    ("GET", "/tuoshu"): PUBLIC,
    ("GET", "/manifest"): PUBLIC,
    ("GET", "/admin"): PUBLIC,
    # 现存表格元数据接口（spec §7.1 漏登，此处补上；前端启动强依赖，保持 public）
    ("GET", "/api/meta"): PUBLIC,
    ("GET", "/api/field_options"): "record:read",
    ("POST", "/api/field_options"): "options:manage",
    ("GET", "/api/version"): PUBLIC,
    ("GET", "/api/train_summary"): AUTHENTICATED,
    ("POST", "/api/train_status"): "train:manage",  # 写入目标表 = train_meta
    ("GET", "/api/service_status"): AUTHENTICATED,
    ("GET", "/api/rows"): "record:read",
    ("POST", "/api/row"): "record:write",
    ("POST", "/api/row/insert"): "record:write",
    ("DELETE", "/api/row/<int:rid>"): "record:write",
    ("GET", "/api/trash"): "record:read",
    ("POST", "/api/trash/<int:rid>/restore"): "record:write",
    ("DELETE", "/api/trash/<int:rid>"): "trash:purge",
    ("PATCH", "/api/row/<int:rid>"): "record:write",
    ("POST", "/api/cells"): "record:write",
    # 写入目标表 = records(dsk/ATB)；认证靠 X-Stamp-Token，门禁 public + CSRF 豁免
    # B 加固：函数体另有 IP 白名单(127.0.0.1/::1)+拒代理头+compare_digest，弱默认已删
    ("POST", "/api/stamp"): PUBLIC,
    ("POST", "/api/price/<int:rid>"): "price:manage",
    ("POST", "/api/price/batch"): "price:manage",
    ("POST", "/api/export"): "record:export",
    ("GET", "/api/tuoshu/meta"): AUTHENTICATED,
    ("POST", "/api/tuoshu/preview"): "tuoshu:generate",
    ("POST", "/api/tuoshu/generate"): "tuoshu:generate",
    ("GET", "/api/tuoshu/dest_map"): AUTHENTICATED,
    ("POST", "/api/tuoshu/dest_map"): "tuoshu:generate",
    ("POST", "/api/import"): "data:import",  # 写入目标表 = records
    ("POST", "/api/import_upload"): "data:import",  # 写入目标表 = records
    ("POST", "/api/manifest/upload"): "manifest:import",
    ("POST", "/api/manifest/apply"): "manifest:apply",
    ("GET", "/api/manifest/batches"): "manifest:import",
    ("GET", "/api/manifest/batch/<batch_id>"): "manifest:import",
    ("POST", "/api/manifest/revert"): "manifest:apply",
    ("POST", "/api/manifest/restore"): "manifest:apply",
    # 两改：暂缓池管理。GET 查看走 manifest:import，POST 变更（撤销/改判）走 manifest:apply，
    # 与 api_manifest_defer 函数体内 _check_manifest_user/_check_manifest_apply 一致。
    ("GET", "/api/manifest/defer"): "manifest:import",
    ("POST", "/api/manifest/defer"): "manifest:apply",
    ("GET", "/api/state"): AUTHENTICATED,
    ("POST", "/api/state"): AUTHENTICATED,
    # admin_api（无 url_prefix，规则即全路径）
    ("GET", "/api/admin/status"): "admin:view",
    ("GET", "/api/admin/logs"): "admin:view",
    ("GET", "/api/admin/table/<key>"): "admin:view",
    ("POST", "/api/admin/table/<key>"): "config:manage",
    ("PATCH", "/api/admin/table/<key>/<record_id>"): "config:manage",
    ("DELETE", "/api/admin/table/<key>/<record_id>"): "config:manage",
    ("GET", "/api/admin/draft_robot_config"): "admin:view",
    ("POST", "/api/admin/draft_robot_config"): "config:manage",
    ("GET", "/api/admin/bot_config/<name>"): "admin:view",
    ("POST", "/api/admin/bot_config/<name>"): "config:manage",
    ("GET", "/api/admin/price"): "price:manage",
    ("POST", "/api/admin/price/exrate"): "price:manage",
    ("POST", "/api/admin/price/entry"): "price:manage",
    ("POST", "/api/admin/price/entry/delete"): "price:manage",
    ("POST", "/api/admin/price/import"): "price:manage",
    ("POST", "/api/admin/price/import/confirm"): "price:manage",
    ("POST", "/api/admin/price/recalc"): "price:manage",
    # 认证模块自身路由（spec §7.4；/api/meta 重名 → 身份接口用 /api/auth_meta）
    ("POST", "/api/login"): PUBLIC,  # 需 CSRF（spec §5.3/§6.1，不豁免）
    ("POST", "/api/logout"): AUTHENTICATED,
    ("GET", "/api/csrf"): PUBLIC,
    ("GET", "/api/auth_meta"): AUTHENTICATED,
    ("POST", "/api/change_password"): AUTHENTICATED,
}

# spec §6.1：模块加载时预构建，供「未登记 → 403」判定
ROUTE_PERMISSIONS_SET = set(ROUTE_PERMISSIONS.keys())
