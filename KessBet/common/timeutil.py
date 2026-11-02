"""common/timeutil.py — 时间转换工具。

**数据库里所有时间都存北京时间（UTC+8），不带时区标记。**

为什么不存 UTC：
  业务侧看数据、写 SQL 分析、对账时都是用北京时间。存 UTC 每次都要
  手动 +8 小时，容易出错（项目里已经有过"直接入库偏 8 小时"的坑）。
  北京无夏令时，转换是固定 +8，没有歧义。

约定：
  - 接口返回的时间戳（mbtp）是绝对时间，转北京时间 = fromtimestamp(ts/1000)
  - 代码内部可以用 aware datetime，但**写库前一律用 to_db()**
"""
from datetime import datetime, timedelta, timezone

# 北京时间 = UTC+8，无夏令时
BEIJING = timezone(timedelta(hours=8), name='Asia/Shanghai')


def to_db(dt: datetime) -> datetime:
    """任意 datetime → 写库用的 naive 北京时间。"""
    if dt.tzinfo is None:
        # 已经是 naive，假定调用方给的就是北京时间
        return dt
    return dt.astimezone(BEIJING).replace(tzinfo=None)


def from_ts(ts_ms: int) -> datetime:
    """接口的毫秒时间戳 → naive 北京时间。"""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(BEIJING).replace(tzinfo=None)


def now_beijing() -> datetime:
    """当前 naive 北京时间。"""
    return datetime.now(BEIJING).replace(tzinfo=None)


def now_aware() -> datetime:
    """当前 aware 北京时间（内部计算用）。"""
    return datetime.now(BEIJING)
