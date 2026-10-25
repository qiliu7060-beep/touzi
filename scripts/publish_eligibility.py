"""CI发布资格：经核验的整日休市保留既有产物；日历未知则失败关闭。"""
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from touzi.market_calendar import trading_day


def publish_eligibility(now=None):
    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    current = current.astimezone(ZoneInfo("Asia/Shanghai"))
    state = trading_day(current.date())
    if state is None:
        raise ValueError("当前年度交易日历尚未核验，停止发布，不自动跳过")
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
    print('已核验交易日：继续原有发布及新鲜度门禁' if allowed else '已核验整日休市：测试继续，保留最近合格Pages产物，不重算或重打数据时间')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
