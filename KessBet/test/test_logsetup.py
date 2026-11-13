"""日志配置的测试。

重点验证两件事：
  1. 按天切分的文件名是 `<name>.log.YYYY-MM-DD`，不是默认的带时分秒
  2. 保留天数配置正确
"""
import logging
import logging.handlers
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common import logsetup


def test_daily_handler_suffix_is_date_only():
    """切分后的文件名只带日期。

    默认 suffix 是 `%Y-%m-%d_%H-%M-%S`，切出来是
    `api.log.2026-11-13_00-00-00`。设成 `%Y-%m-%d` 才是 `api.log.2026-11-13`。
    """
    h = logsetup.daily_handler(os.path.join(BASE, 'logs', '_t.log'),
                               backupCount=7)
    try:
        assert h.suffix == '%Y-%m-%d'
        assert h.backupCount == 7
        assert h.when == 'MIDNIGHT'
    finally:
        h.close()
        p = os.path.join(BASE, 'logs', '_t.log')
        if os.path.isfile(p):
            os.remove(p)


def test_daily_handler_accepts_dictconfig_style_kwargs():
    """uvicorn 的 dictConfig 会把 JSON 里的键当 kwargs 传进来。

    构造函数只接受 filename / when / backupCount / encoding 等，
    `suffix` 是属性 —— 直接写进 JSON 会 TypeError。
    这个工厂函数就是为此存在的。
    """
    h = logsetup.daily_handler(
        os.path.join(BASE, 'logs', '_t2.log'),
        backupCount=30, encoding='utf-8')
    try:
        assert isinstance(h, logging.handlers.TimedRotatingFileHandler)
    finally:
        h.close()
        p = os.path.join(BASE, 'logs', '_t2.log')
        if os.path.isfile(p):
            os.remove(p)


def test_setup_writes_to_file_when_not_tty(monkeypatch):
    """非终端环境（被启动脚本拉起）时写文件，不写 stderr。"""
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: False, raising=False)
    path = logsetup.setup('_t_setup', verbose=False)
    try:
        assert path and os.path.isfile(path)
        logging.getLogger('x').info('hello')
        for h in logging.getLogger().handlers:
            h.flush()
        content = open(path, encoding='utf-8').read()
        assert 'hello' in content
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        if path and os.path.isfile(path):
            os.remove(path)


def test_setup_does_not_duplicate_handlers(monkeypatch):
    """重复调用 setup 不能把日志打两遍。"""
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: False, raising=False)
    logsetup.setup('_t_dup')
    n1 = len(logging.getLogger().handlers)
    logsetup.setup('_t_dup')
    n2 = len(logging.getLogger().handlers)
    try:
        assert n2 == n1 == 1
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            h.close()
            root.removeHandler(h)
        p = os.path.join(logsetup.LOG_DIR, '_t_dup.log')
        if os.path.isfile(p):
            os.remove(p)
