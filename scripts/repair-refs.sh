#!/bin/sh
# repair-remote-refs.sh — 修复本机 git(WorkBuddy PortableGit 2.54.0) 对
# refs/remotes/* 写入静默失效的可靠手段。
#
# 根因：该 git 改写 .git/packed-refs（unlink 旧文件 + rename 覆盖）在本机静默
# 失败（疑似 Windows Defender 拦截删除/重命名 .git/packed-refs），导致每次
# fetch / pull 推进 dev 后 origin/dev、origin/main 变 [gone] 或陈旧。OS 层
# rename 与直接写文件都正常，heads 的 loose 写也正常，唯独 packed-refs 重写坏。
#
# 本脚本直接用 printf 把远端跟踪引用写回正确值（绕过 git 坏路径）。
# 2026-11-06 扩展：本地 `refs/heads/main` 缺失或内容非法时，也用远端 main 恢复
#              （只在缺失/非法时动手，本地领先远端绝不覆盖）。
# 用法：sh scripts/repair-refs.sh [remote]   （默认 origin）
# 由 git alias "sync" 在 fetch 之后自动调用；也可手动运行。
# 体检请用：python scripts/git_guard.py

remote="$1"
[ -z "$remote" ] && remote="origin"

gitdir="$(git rev-parse --git-dir)" || exit 0

# 离线/认证失败不要破坏本次操作：静默退出
refs="$(git ls-remote --refs "$remote" 'refs/heads/*' 2>/dev/null)" || true
if [ -z "$refs" ]; then
    # 拿不到远端时「不写」是对的（避免用空数据覆盖）；但如果本地 main 也缺，
    # 静默 exit 0 会让人以为修好了 —— 这种情况必须明确报错。
    if [ ! -s "$gitdir/refs/heads/main" ]; then
        echo "[repair-refs] 取不到远端 $remote 的 refs，且本地 refs/heads/main 缺失；未做任何修改" >&2
        echo "[repair-refs] 先确认网络/代理（见 WORKFLOW §10），再重跑本脚本" >&2
        exit 1
    fi
    exit 0
fi

printf '%s\n' "$refs" | while read -r sha ref; do
    [ -z "$sha" ] && continue
    branch="${ref#refs/heads/}"
    # 注意 branch 可能是 "chore/repo-hygiene" 这种带斜杠的（历史遗留），
    # mkdir 必须建到它的父目录，否则写文件会报 No such file or directory。
    mkdir -p "$gitdir/refs/remotes/$remote/$(dirname "$branch")"
    printf '%s\n' "$sha" > "$gitdir/refs/remotes/$remote/$branch"
done

# ---- 顺带：本地 refs/heads/main 缺失或内容非法时，用远端 main 恢复 ----
# 只在「缺失 / 不是 40 位 hex」时动手；本地领先远端（合法 SHA）绝不覆盖。
# ⚠️ 必须用 `git update-ref` 写，**不要用 printf/cp 直接写 ref 文件**：
#    2026-11-06 实测 `cp /tmp/xxx .git/refs/heads/main` 会返回 0 但文件被静默抹掉，
#    同一条数据用 `git update-ref` 写才持久。（cmd 的 `>` 同样不可靠）
main_sha="$(printf '%s\n' "$refs" | awk '$2=="refs/heads/main"{print $1; exit}')"
if [ -n "$main_sha" ]; then
    cur="$(cat "$gitdir/refs/heads/main" 2>/dev/null)"
    case "$cur" in
        [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) : ;;  # 看着正常，不动
        *)
            git update-ref refs/heads/main "$main_sha" 2>/dev/null
            if [ -s "$gitdir/refs/heads/main" ]; then
                printf '[repair-refs] restored refs/heads/main -> %s\n' "$main_sha" >&2
            else
                printf '[repair-refs] 写入 refs/heads/main 失败（update-ref 返回 0 但没落盘）\n' >&2
                printf '[repair-refs] 这个 .git 已经不可靠，建议重新 clone 只换 .git（见 WORKFLOW §10）\n' >&2
                exit 1
            fi
            ;;
    esac
fi

exit 0
