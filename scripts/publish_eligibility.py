"""CI发布资格：休市或盘前收盘输入已超时则保留既有产物；日历未知失败关闭。"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from touzi.market_calendar import trading_day, adjacent_day


def publish_eligibility(now=None):
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    current = current.astimezone(ZoneInfo("Asia/Shanghai"))
    state = trading_day(current.date())
    if state is None:
        raise ValueError("当前年度交易日历尚未核验，停止发布，不自动跳过")
    if state and (current.hour, current.minute) < (9, 30):
        previous = adjacent_day(current.date(), -1)
        if previous is None:
            raise ValueError("盘前最近交易日尚未核验，停止发布，不推测收盘输入")
        # 周一/长假后盘前仍使用上次收盘；不以新请求时间绕过生产者36小时保护。
        previous_close = datetime.combine(previous, datetime.min.time(), current.tzinfo) + timedelta(hours=15)
        if current - previous_close > timedelta(hours=36):
            return False
    return state


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    allowed = publish_eligibility()
    value = 'true' if allowed else 'false'
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as output:
            output.write(f'publish={value}\n')
    print('已核验交易日有效窗口：继续原有发布及新鲜度门禁' if allowed else '已核验休市或盘前收盘输入已超时：测试继续，保留最近合格Pages产物，不重算或重打数据时间')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
