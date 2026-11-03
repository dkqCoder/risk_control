"""deploy 脚本的行为测试。

覆盖 2026-11-02 复核出的四个缺陷：
  1. stop.sh 只信 pidfile 里的数字，PID 被系统复用后会误杀无关进程
  2. stop.sh 在无权限发信号时把 pidfile 删掉，导致进程变孤儿、可能被重复启动
  3. start.sh 同样只信 pidfile，PID 复用时会误判"已在运行"而拒绝启动
  4. 采集/分析进程只捕获 KeyboardInterrupt，SIGTERM（stop.sh 发的信号）
     不会触发清理，finally 不执行

测试通过真实子进程跑，不 mock 业务逻辑。
"""
import os
import shutil
import signal
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(BASE, 'deploy')


def _make_pkg(tmp_path):
    """在临时目录里铺一份 deploy/，让脚本按自己的位置定位 PKG_DIR。"""
    pkg = tmp_path / 'pkg'
    (pkg / 'deploy').mkdir(parents=True)
    (pkg / 'run').mkdir()
    (pkg / 'logs').mkdir()
    for name in os.listdir(DEPLOY):
        if name.endswith('.sh'):
            shutil.copy(os.path.join(DEPLOY, name), pkg / 'deploy' / name)
    return pkg


def _spawn_sleeper(module=None):
    """起一个 sleep 进程。module 非空时把模块名塞进命令行，
    让 pid_is_ours() 认出它是"自己的"进程。

    用 python 而不是 bash：bash 对最后一条命令会 exec 替换自身，
    替换后命令行里就没有尾部参数了。
    """
    if module is None:
        return subprocess.Popen(['sleep', '300'])
    return subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(300)', module])


def _kill_quietly(p):
    try:
        p.kill()
        p.wait(timeout=5)
    except Exception:
        pass


TEST_SCRIPT = """
import sys
sys.path.insert(0, {base!r})
import {module} as m
m.install_signal_handlers()
try:
    import time
    time.sleep(60)
except KeyboardInterrupt:
    print("CLEANUP_RAN", flush=True)
    sys.exit(0)
"""


def _check_sigterm(module):
    code = TEST_SCRIPT.format(base=BASE, module=module)
    p = subprocess.Popen([sys.executable, '-c', code],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    try:
        time.sleep(1.5)                 # 等它装好 handler
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=20)
    finally:
        _kill_quietly(p)
    return out, err, p.returncode


# ---------- 1. 不误杀无关进程 ----------

def test_stop_does_not_kill_recycled_pid(tmp_path):
    """pidfile 里的 pid 属于无关进程时，stop.sh 不能杀它。"""
    pkg = _make_pkg(tmp_path)
    victim = _spawn_sleeper()
    try:
        (pkg / 'run' / 'collect.pid').write_text(str(victim.pid))

        r = subprocess.run(['bash', str(pkg / 'deploy' / 'stop.sh')],
                           capture_output=True, text=True, timeout=30)

        assert victim.poll() is None, 'stop.sh 误杀了无关进程'
        assert not (pkg / 'run' / 'collect.pid').exists(), '失效 pidfile 应被清理'
        assert '无关' in (r.stdout + r.stderr) or 'collect.run' in (r.stdout + r.stderr)
    finally:
        _kill_quietly(victim)


# ---------- 2. 无权限时不删 pidfile ----------

def test_stop_keeps_pidfile_when_signal_fails(tmp_path):
    """发信号失败（如跨用户 EPERM）时必须保留 pidfile 并报错。"""
    pkg = _make_pkg(tmp_path)
    victim = _spawn_sleeper(module='collect.run')
    try:
        (pkg / 'run' / 'collect.pid').write_text(str(victim.pid))

        # 用一个 shell 函数覆盖 kill 内建：kill -TERM 必失败，其余透传。
        # 不能靠 PATH 里的假 kill——kill 是 bash 内建命令，不走 PATH。
        harness = (
            'set -euo pipefail\n'
            f'kill() {{ if [ "$1" = "-TERM" ]; then return 1; fi; '
            'builtin kill "$@"; }\n'
            f'source {str(pkg / "deploy" / "stop.sh")!r}\n'
            'RUN_DIR=$1; stop_one collect\n'
        )
        r = subprocess.run(['bash', '-c', harness, 'x', str(pkg / 'run')],
                           capture_output=True, text=True, timeout=30)

        assert r.returncode != 0, '发信号失败时应返回非 0'
        assert (pkg / 'run' / 'collect.pid').exists(), \
            '发信号失败时不能删 pidfile（否则进程变孤儿，可能被重复启动）'
        assert '权限' in (r.stdout + r.stderr)
    finally:
        _kill_quietly(victim)


# ---------- 3. start.sh 不误判"已在运行" ----------

def test_start_ignores_recycled_pid(tmp_path):
    """pidfile 指向无关进程时，start.sh 应照常启动而不是跳过。"""
    pkg = _make_pkg(tmp_path)

    fake_py = pkg / '.venv' / 'bin' / 'python'
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text('#!/bin/bash\nexec sleep 300\n')
    fake_py.chmod(0o755)
    (pkg / 'deploy' / '.env').write_text('KESSBET_DB_PASSWORD=x\n')

    unrelated = _spawn_sleeper()
    started_pid = None
    try:
        (pkg / 'run' / 'collect.pid').write_text(str(unrelated.pid))

        r = subprocess.run(['bash', str(pkg / 'deploy' / 'start.sh'), 'collect'],
                           capture_output=True, text=True, timeout=30)
        out = r.stdout + r.stderr

        assert '已在运行' not in out, '把无关进程当成了自己的进程'
        assert '已启动' in out, f'没有启动：{out}'

        started_pid = int((pkg / 'run' / 'collect.pid').read_text())
        assert started_pid != unrelated.pid
    finally:
        _kill_quietly(unrelated)
        if started_pid:
            try:
                os.kill(started_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------- 4. SIGTERM 触发清理 ----------

def test_collect_handles_sigterm():
    out, err, rc = _check_sigterm('collect.run')
    assert 'CLEANUP_RAN' in out, f'SIGTERM 未触发清理，stdout={out!r}'
    assert 'Traceback' not in err, f'SIGTERM 抛出未捕获异常：{err}'
    assert rc == 0


def test_analyze_handles_sigterm():
    out, err, rc = _check_sigterm('analyze.run')
    assert 'CLEANUP_RAN' in out, f'SIGTERM 未触发清理，stdout={out!r}'
    assert 'Traceback' not in err, f'SIGTERM 抛出未捕获异常：{err}'
    assert rc == 0
