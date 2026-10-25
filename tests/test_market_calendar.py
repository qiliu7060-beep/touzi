"""交易所日历与生产门禁边界，不依赖网络。"""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from touzi.market_calendar import quote_state, model_expiry, market_clock


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai'))


class CalendarTests(unittest.TestCase):
    def test_python_worker_matrix(self):
        # 直接读取Node同一矩阵，防止各语言只测试各自预期。
        import json, re
        text = (Path(__file__).with_suffix('.mjs')).read_text(encoding='utf-8')
        matrix = json.loads('[' + re.search(r'const cases = \[(.*?)\n\];', text, re.S)[1].replace("'", '"') + ']')
        for now, quote, expected in matrix:
            with self.subTest(now=now, quote=quote):
                self.assertEqual(quote_state(stamp(quote), stamp(now)), expected)

    def test_model_expiry_and_unknown_calendar(self):
        self.assertEqual(model_expiry(stamp('2026-10-23T23:51:22'), stamp('2026-10-23T16:15:00')),stamp('2026-10-26T09:30:00'))
        self.assertIsNone(market_clock(stamp('2027-01-01T10:00:00')))


if __name__ == '__main__':
    unittest.main()
