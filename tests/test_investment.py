import unittest
from datetime import datetime
import pandas as pd
from touzi.investment import allocate, bond_signal
from touzi.market_calendar import TZ, trading_day

class InvestmentTests(unittest.TestCase):
    def test_allocation(self):
        for p,q,expected in [(2/3,1,(200000,100000,0)),(.7,1,(210000,90000,0)),(.5,1,(150000,100000,50000)),(.5,.5,(150000,50000,100000))]:
            self.assertEqual(tuple(allocate('3000',p,60,q).values()),expected)
        self.assertEqual(allocate('3000',.5,30,0),dict(stock=0,bond=0,cash=300000))
        for amount in ['0','0.01','1.23','100000000']:
            result=allocate(amount,.547,48.54,.75)
            from decimal import Decimal
            self.assertEqual(sum(result.values()),int(Decimal(amount)*100))
        for bad in ['-1','1e3','NaN','1.001','100000001','']:
            with self.assertRaises(ValueError): allocate(bad,.5,60,1)
    def test_cross_language_precision(self):
        import json, subprocess
        positions = [2/3, .1234567, .1234565, .9999998, .547, 0, 1]
        cases = [[amount,p,score,q] for amount in ['0','0.01','3000','100000000'] for p in positions for score in [30,60] for q in [0,.5,.75,1]]
        script = "import {investmentAllocation} from './scripts/investment-browser.mjs';let raw='';for await(const chunk of process.stdin)raw+=chunk;console.log(JSON.stringify(JSON.parse(raw).map(args=>investmentAllocation(...args))));"
        result = subprocess.run(['node','--input-type=module','-e',script], input=json.dumps(cases),text=True,capture_output=True,check=True)
        self.assertEqual(json.loads(result.stdout),[allocate(*args) for args in cases])

    def test_yield_boundaries_and_long_pressure(self):
        days=[d for d in pd.date_range('2026-06-01','2026-10-30') if trading_day(d.date())][-65:]
        now=datetime(2026,10,30,19,tzinfo=TZ)
        rules=dict(caution_bp=5,pause_bp=15,long_pressure_bp=15)
        for delta,expected in [(0,1),(4.999,.75),(5,.5),(14.999,.5),(15,0)]:
            values=[2.0]*65
            values[-5:]=[2+delta/100]*5
            self.assertEqual(bond_signal(pd.Series(values,index=days),now,rules)['factor'],expected)
        values=[2.0]*65
        values[-65:-60]=[1.8]*5
        self.assertEqual(bond_signal(pd.Series(values,index=days),now,rules)['factor'],.5)

    def test_direction(self):
        days=[d for d in pd.date_range('2026-06-01','2026-10-30') if trading_day(d.date())]
        now=datetime(2026,10,30,19,tzinfo=TZ)
        rules=dict(caution_bp=5,pause_bp=15,long_pressure_bp=15)
        self.assertEqual(bond_signal(pd.Series([2-i*.01 for i in range(len(days))],index=days),now,rules)['factor'],1)
        self.assertEqual(bond_signal(pd.Series([1+i*.01 for i in range(len(days))],index=days),now,rules)['factor'],0)
        with self.assertRaises(ValueError): bond_signal(pd.Series([1]*10,index=days[:10]),now,rules)
        with self.assertRaises(ValueError): bond_signal(pd.Series([1],index=[pd.Timestamp('2026-11-02')]),now,rules)

if __name__=='__main__': unittest.main()
