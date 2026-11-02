"""pytest 全局配置：把 deploy/.env 里的数据库配置载入环境变量。

数据库密码不再硬编码在代码里，所以本地跑测试前必须有一份
deploy/.env（不进 git）。没有该文件时测试会自己 skipp 掉需要库的用例，
而不是以 RuntimeError 的形式炸掉整个套件。

用法：
    cp deploy/env.example deploy/.env   # 然后填真实密码
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / 'deploy' / '.env'


def _load_env(path: Path) -> None:
    """最小实现的 .env 加载：KEY=VALUE，忽略注释和空行。

    不引入 python-dotenv，避免给部署增加一个依赖。
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # 已存在的环境变量优先（CI / 部署脚本可以覆盖）
        os.environ.setdefault(key, value)


_load_env(ENV_FILE)
