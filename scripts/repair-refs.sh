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
# 用法：sh scripts/repair-refs.sh [remote]   （默认 origin）
# 由 git alias "sync" 在 fetch 之后自动调用；也可手动运行。

remote="$1"
[ -z "$remote" ] && remote="origin"

gitdir="$(git rev-parse --git-dir)" || exit 0

# 离线/认证失败不要破坏本次操作：静默退出
refs="$(git ls-remote --refs "$remote" 'refs/heads/*' 2>/dev/null)" || exit 0
[ -z "$refs" ] && exit 0

printf '%s\n' "$refs" | while read -r sha ref; do
    [ -z "$sha" ] && continue
    branch="${ref#refs/heads/}"
    mkdir -p "$gitdir/refs/remotes/$remote"
    printf '%s\n' "$sha" > "$gitdir/refs/remotes/$remote/$branch"
done

exit 0
