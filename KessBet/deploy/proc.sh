#!/usr/bin/env bash
# =============================================================================
# deploy/proc.sh — start.sh / stop.sh 共用的进程校验
# =============================================================================
# 由其他脚本 source，不单独执行。
#
# 为什么需要它：pidfile 里只有一串数字。PID 会被系统回收复用，直接
# `kill -0 $pid` 判断"是否存活"会把毫不相干的进程当成自己的：
#   - stop.sh 会误杀那个进程
#   - start.sh 会以为"已在运行"而拒绝启动
# 所以必须再核对进程的真实命令行里是否包含预期模块名。
# =============================================================================

# pid_is_ours <pid> <module>
#   进程存在、且命令行包含 module（如 collect.run）才返回 0。
#   无权限读取 /proc/<pid>/cmdline（别的用户的进程）时返回 1——
#   此时既不能确认是自己的，也不该把它当成自己的。
pid_is_ours() {
    local pid="$1" module="$2"
    [ -n "$pid" ] || return 1
    [ -r "/proc/$pid/cmdline" ] || return 1
    # cmdline 用 NUL 分隔，转成空格再匹配
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q -- "$module"
}

# pid_alive <pid>
#   仅判断进程是否存活（不校验归属）。
pid_alive() {
    local pid="$1"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
