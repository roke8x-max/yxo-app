#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git 仓库健康守卫 —— 在 `.git` 半损坏时也能跑。

为什么需要它
------------
本机 git（WorkBuddy PortableGit）有已知缺陷：**改写 `.git/packed-refs` 会静默失败**
（unlink + rename 被安全软件拦截，见 `scripts/repair-refs.sh` 的注释）。
失败时 git 仍然返回 0、仍然打印 "Switched to a new branch"，但 **ref 根本没落盘**。
在这个状态下继续 `git add` / `commit`，会把提交挂在一个不存在的 HEAD 上，
症状是「整仓文件都显示成 A（新增）」，严重时 refs 目录被清空、pack 文件丢失。

2026-11-06 就是这么坏了一次（用 `git checkout -b chore/repo-hygiene` 建的带斜杠分支）。

用法
----
    python scripts/git_guard.py            # 检查，全绿 exit 0，有问题 exit 1
    python scripts/git_guard.py --quiet    # 只输出问题

**什么时候跑**（写死，别省）：
  1. 建完分支、切过去之后   ← 最重要，这次就是漏了这步
  2. `git commit` 之前
  3. `git push` 之前
  4. 任何 git 命令返回非 0 或者行为古怪时

设计约束：**核心检查只用文件系统**，不依赖 `git` 命令能否跑通
（.git 坏掉时 git 命令本来就不可靠）。只有「工作区是否异常」这一项需要 git。
"""
import os
import re
import sys
import subprocess

OK = "[OK]  "
WARN = "[WARN]"
BAD = "[BAD] "

HEX40 = re.compile(r"^[0-9a-f]{40}$")


def find_gitdir(start=None):
    """从当前目录往上找 .git（文件或目录都算，支持 worktree）。"""
    d = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(d, ".git")
        if os.path.isdir(cand):
            return cand
        if os.path.isfile(cand):
            # worktree: .git 是个文本文件，内容 "gitdir: /path/to/xxx"
            try:
                txt = open(cand, encoding="utf-8").read().strip()
                if txt.startswith("gitdir:"):
                    return txt.split(":", 1)[1].strip()
            except Exception:
                pass
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return None


def main():
    quiet = "--quiet" in sys.argv
    problems = []
    notes = []

    def add(level, msg, fix=""):
        line = f"{level} {msg}"
        if fix:
            line += f"\n        → {fix}"
        notes.append(line)
        if level in (BAD, WARN):
            problems.append((level, msg))

    gitdir = find_gitdir()
    if not gitdir:
        print(f"{BAD} 找不到 .git（当前目录往上没有）")
        return 1

    add(OK, f"git 目录：{gitdir}")

    # ---- 1. HEAD 必须指向一个真实存在的 ref（这次事故的直接原因）----
    head = read(os.path.join(gitdir, "HEAD"))
    cur_branch = None
    if head is None:
        add(BAD, "读不到 .git/HEAD", "仓库已损坏，按 WORKFLOW §3.1 救援")
        return _report(notes, problems, quiet)

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        cur_branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        ref_path = os.path.join(gitdir, ref.replace("/", os.sep))
        sha = read(ref_path)
        if sha is None:
            add(BAD, f"HEAD 指向 {ref}，但这个 ref 文件不存在",
                "git symbolic-ref HEAD refs/heads/main  ← 先修 HEAD，再重建分支（用 update-ref + 扁平名）")
        elif not HEX40.match(sha):
            add(BAD, f"{ref} 的内容不是合法 SHA：{sha[:40]!r}",
                "ref 文件被写坏了，用 main 的 SHA 重写：git update-ref " + ref + " <main的SHA>")
        else:
            add(OK, f"HEAD -> {ref} = {sha[:8]}")
    else:
        add(WARN, f"HEAD 是游离状态（detached）：{head[:12]}",
            "git checkout main 回到分支上再干活")

    # ---- 2. 分支名不能带斜杠（本机 git 对嵌套 ref 目录会静默抹掉）----
    if cur_branch and "/" in cur_branch:
        add(BAD, f"当前分支名带斜杠：{cur_branch}",
            "立刻切回 main，用扁平名重建（连字符，绝不用斜杠）")

    # ---- 3. refs/heads 全量体检 ----
    heads_dir = os.path.join(gitdir, "refs", "heads")
    if not os.path.isdir(heads_dir):
        add(BAD, "refs/heads 目录不存在",
            "仓库引用已损坏；git symbolic-ref HEAD refs/heads/main + 用 update-ref 重建分支")
    else:
        found = []
        broken = []
        for dp, _dn, fn in os.walk(heads_dir):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, heads_dir).replace(os.sep, "/")
                v = read(full)
                if v is None or not HEX40.match(v):
                    broken.append(rel)
                else:
                    found.append(rel)
        if broken:
            add(BAD, f"refs/heads 下 {len(broken)} 个 ref 内容非法：{', '.join(broken[:5])}",
                "删掉坏 ref（git update-ref -d refs/heads/<名>），用 main 重建需要的分支")
        # main 是唯一的真相分支，它必须存在
        if "main" not in found:
            add(BAD, "refs/heads/main 不存在",
                "git update-ref refs/heads/main <远端 main 的 SHA>（用 git ls-remote 拿）")
        add(OK, f"refs/heads：{found if found else '（空）'}")

    # ---- 4. pack 完整性：每个 .idx 必须有同名 .pack ----
    pack_dir = os.path.join(gitdir, "objects", "pack")
    if os.path.isdir(pack_dir):
        names = os.listdir(pack_dir)
        idx = {n[:-4] for n in names if n.endswith(".idx")}
        pak = {n[:-5] for n in names if n.endswith(".pack")}
        orphan_idx = idx - pak
        if orphan_idx:
            add(BAD, f"{len(orphan_idx)} 个 .idx 没有对应 .pack（对象数据丢失）",
                "重新 clone 一份，只替换 .git（工作区文件不动）—— 见 WORKFLOW §10")
        elif pak:
            add(OK, f"pack：{len(pak)} 个 pack / {len(idx)} 个 idx，配对完整")
        else:
            add(OK, "pack：（无，全部是散对象）")
    else:
        add(OK, "pack 目录不存在（全部散对象，正常）")

    # ---- 5. index.lock 残留 ----
    lock = os.path.join(gitdir, "index.lock")
    if os.path.exists(lock):
        try:
            age = os.path.getmtime(lock)
            import time
            secs = time.time() - age
            add(BAD if secs > 60 else WARN,
                f"存在 index.lock（{int(secs)} 秒前创建）",
                "先确认没有 git 进程在跑（任务管理器看 git.exe）；确认没有才删 lock")
        except Exception:
            add(WARN, "存在 index.lock", "确认无 git 进程后再删")
    else:
        add(OK, "无 index.lock")

    # ---- 6. 工作区异常（需要 git，失败不算问题）----
    try:
        r = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                           text=True, timeout=30, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            added = [l for l in lines if l.startswith("A ")]
            deleted = [l for l in lines if l.startswith(" D")]
            if len(added) > 50:
                add(BAD, f"工作区有 {len(added)} 个文件显示为「新增」",
                    "典型 HEAD 悬空症状！立刻停下，修 HEAD（本脚本第 1 项）")
            elif added:
                add(WARN, f"工作区有 {len(added)} 个「新增」文件（少量可能是正常的）")
            if deleted:
                add(BAD, f"工作区有 {len(deleted)} 个文件显示「已删除」",
                    "多半是 checkout 被打断；git restore -- . 整树恢复，然后重跑本脚本")
            if not added and not deleted:
                add(OK, f"工作区：{len(lines)} 个改动，无异常批量 A/D")
        else:
            add(WARN, "git status 跑不通（.git 可能已损坏）", "以本脚本前 5 项为准")
    except Exception as e:
        add(WARN, f"git status 调用失败：{type(e).__name__}", "以本脚本前 5 项为准")

    return _report(notes, problems, quiet)


def _report(notes, problems, quiet):
    if quiet:
        for n in notes:
            if n.startswith(BAD) or n.startswith(WARN):
                print(n)
    else:
        print("=" * 66)
        print("git 健康检查")
        print("=" * 66)
        for n in notes:
            print(n)
        print("=" * 66)

    bad = [p for p in problems if p[0] == BAD]
    warn = [p for p in problems if p[0] == WARN]
    if bad:
        print(f"\n❌ {len(bad)} 个必须修的项 —— 现在继续操作会损坏仓库")
        return 1
    if warn:
        print(f"\n⚠️  {len(warn)} 个提醒，确认可以继续再动手")
        return 0
    print("\n✅ 全部通过，可以继续")
    return 0


if __name__ == "__main__":
    sys.exit(main())
