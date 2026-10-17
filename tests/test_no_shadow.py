#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""防再发：框架对象名遮蔽静态检查（taskbook A4.2）。

背景：`api_tuoshu_generate` 里用 `for g in groups` 覆盖了 Flask 请求上下文
对象 `g`，函数末尾 `g.identity.username` 于是读到班列 dict，报
`'dict' object has no attribute 'identity'`，生成托书全 500。

本测试用 `ast` 断言：任何**函数作用域**内都不存在名为
`g` / `request` / `session` / `app` 的局部绑定。

检查的绑定形态（target 递归展开，`Tuple`/`List`/`Starred` 均收集其中 Name）：
  - 函数参数（args / kwonlyargs / posonlyargs / vararg / kwarg）
  - Assign / AnnAssign / AugAssign 的目标
  - For / AsyncFor 的 target
  - With / AsyncWith 的 optional_vars
  - ExceptHandler 的 name
  - NamedExpr（海象）的目标

排除：列表/集合/字典推导式与生成器表达式的目标（Python 3 独立作用域，
不遮蔽外层）；`import` 语句（`from flask import g` 是正常用法）；模块顶层
与类体。

扫描范围动态判定：`app.py`、`auth/*.py` 始终在内；此外凡源码含
`import flask` / `from flask import` 的 `.py` 文件都自动罩住。这样将来引入
flask 的模块会被自动覆盖，不碰 flask 的纯引擎模块不被误伤。
"""
import ast
import os
import re

FORBIDDEN = {"g", "request", "session", "app"}
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMP_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_FLASK_RE = re.compile(r"^\s*(import flask\b|from flask import\b)", re.M)
_SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules",
              ".pytest_cache", "build", "dist", ".mypy_cache", ".ruff_cache"}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sites_from_target(target):
    """把赋值 target 递归展开为 [(lineno, name), ...]。"""
    if isinstance(target, ast.Name):
        return [(target.lineno, target.id)]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for e in target.elts:
            out += _sites_from_target(e)
        return out
    if isinstance(target, ast.Starred):
        return _sites_from_target(target.value)
    return []


def _collect_bindings(node, acc):
    """递归收集 node 下的函数作用域绑定；遇嵌套作用域/推导式作用域即停。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_NODES) or isinstance(child, _COMP_NODES):
            continue
        if isinstance(child, ast.Assign):
            for t in child.targets:
                acc += _sites_from_target(t)
        elif isinstance(child, ast.AnnAssign):
            if child.target is not None:
                acc += _sites_from_target(child.target)
        elif isinstance(child, ast.AugAssign):
            acc += _sites_from_target(child.target)
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            acc += _sites_from_target(child.target)
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            for item in child.items:
                if item.optional_vars is not None:
                    acc += _sites_from_target(item.optional_vars)
        elif isinstance(child, ast.ExceptHandler):
            if child.name:
                acc.append((child.lineno, child.name))
        elif isinstance(child, ast.NamedExpr):
            acc += _sites_from_target(child.target)
        _collect_bindings(child, acc)


class _FuncScan(ast.NodeVisitor):
    def __init__(self):
        self.hits = []

    def _check(self, node):
        sites = []
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            sites.append((arg.lineno, arg.arg))
        if a.vararg:
            sites.append((a.vararg.lineno, a.vararg.arg))
        if a.kwarg:
            sites.append((a.kwarg.lineno, a.kwarg.arg))
        _collect_bindings(node, sites)
        for lineno, name in sites:
            if name in FORBIDDEN:
                self.hits.append((lineno, node.name, name))

    def visit_FunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check(node)
        self.generic_visit(node)


def scan_source(src, filename="<src>"):
    """扫描单段源码，返回 [(lineno, func, name), ...]。"""
    tree = ast.parse(src, filename=filename)
    visitor = _FuncScan()
    visitor.visit(tree)
    return visitor.hits


def _iter_py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _select_files(root):
    files = []
    for path in _iter_py_files(root):
        rel = os.path.relpath(path, root).replace("\\", "/")
        try:
            src = open(path, encoding="utf-8").read()
        except Exception:
            continue
        forced = rel == "app.py" or re.match(r"^auth/[^/]+\.py$", rel)
        if forced or _FLASK_RE.search(src):
            files.append((rel, path, src))
    return files


def scan_repo(root=_ROOT):
    """扫描全仓，返回 [(rel, lineno, func, name), ...]。"""
    hits = []
    for rel, path, src in sorted(_select_files(root)):
        for lineno, func, name in scan_source(src, filename=path):
            hits.append((rel, lineno, func, name))
    return hits


def test_no_framework_name_shadowing():
    hits = scan_repo()
    assert hits == [], (
        "函数作用域内出现框架对象名遮蔽（会遮蔽 Flask 的 g/request/session/app）：\n" +
        "\n".join(f"  {rel}:{lineno} func={func} name={name}"
                  for rel, lineno, func, name in hits))


def test_scanner_is_not_vacuous():
    """证伪保护：采样各种绑定形态，确认扫描器真的能抓到（否则本文件形同虚设）。"""
    src = (
        "def f(g):\n"                     # 参数
        "    for g, x in items:\n"        # Tuple target
        "        pass\n"
        "    a = b = g\n"                 # 多 target 链式赋值
        "    g += 1\n"                    # AugAssign
        "    with open('x') as app:\n"    # With optional_vars
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as session:\n"  # ExceptHandler name
        "        pass\n"
        "    request = 1\n"               # Assign
        "    if (g := 2):\n"              # NamedExpr
        "        pass\n"
        "    return [g for g in items]\n"  # 推导式目标不得被算作函数作用域绑定
    )
    hits = scan_source(src)
    flagged = {(func, name) for _, func, name in hits}
    assert ("f", "g") in flagged
    assert ("f", "app") in flagged
    assert ("f", "session") in flagged
    assert ("f", "request") in flagged
    # 推导式 for 目标 `g` 若被错误计入，只会让 `f,g` 依旧出现（无法区分），
    # 故单独验证：纯推导式函数不应命中。
    assert scan_source("def h(xs):\n    return [g for g in xs]\n") == []


def test_scan_scope_includes_app_and_auth():
    """范围守门：app.py 与 auth/*.py 必须始终在扫描集内。"""
    rels = {rel for rel, _, _ in _select_files(_ROOT)}
    assert "app.py" in rels
    assert "auth/__init__.py" in rels
    assert "auth/rbac.py" in rels
