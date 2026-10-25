import assert from 'node:assert/strict';
import { financialCoverage } from '../scripts/financial-coverage.mjs';
const row = {revenue:100,operating_profit:20,net_profit:10,operating_cash_flow:12,capex:3,deducted_parent_net_profit:null};
const entries = {
  '000001':{industry:'金融行业',financial_analysis:{status:'ok',quality:{coverage:.65},years:Array(5).fill(row),metrics:{debt_equity:{status:'not_applicable'},capex_to_profit:{status:'not_applicable'},annual_net_profit:{status:'ok'},cumulative_net_profit:{status:'ok'}}}},
  '300750':{industry:'',financial_analysis:{status:'partial',quality:{coverage:.65},years:Array(5).fill(row),metrics:{debt_equity:{status:'missing'},capex_to_profit:{status:'missing'},annual_net_profit:{status:'ok'},cumulative_net_profit:{status:'ok'}}}},
  '600001':{industry:'制造业',financial_analysis:{status:'ok',quality:{coverage:1},years:Array(5).fill({...row,net_profit:-10}),metrics:{debt_equity:{status:'ok'},capex_to_profit:{status:'invalid'},annual_net_profit:{status:'ok'},cumulative_net_profit:{status:'ok'}}}},
  '688825':{industry:'制造业',financial_analysis:{status:'partial',quality:{coverage:.5},years:Array(4).fill(row),metrics:{debt_equity:{status:'ok'},capex_to_profit:{status:'missing'},annual_net_profit:{status:'ok'},cumulative_net_profit:{status:'missing'}}}}
};
const actual = financialCoverage(entries);
assert.deepEqual(actual,{total:4,reports:{ok:2,partial:2},metrics:{debt_equity:{not_applicable:1,missing:1,ok:2},capex_to_profit:{not_applicable:1,missing:2,invalid:1},annual_net_profit:{ok:4},cumulative_net_profit:{ok:3,missing:1}},quality:{full:1,partial:3,missing:0},fields:{expected:20,deducted_parent_missing:20},reasons:{industry_unknown:1,amounts_missing:1}});
assert.equal(financialCoverage({}).total,0);
assert.equal(financialCoverage({'000001':{}}).metrics.debt_equity.missing,1);
assert.equal(financialCoverage({'000001':{financial_analysis:{quality:{coverage:Infinity}}}}).quality.missing,1);
if (process.argv.includes('--json')) console.log(JSON.stringify({entries,actual}));
else console.log('financial coverage: null/zero, industry unknown, exemptions, invalid profit and missing year are distinct');
