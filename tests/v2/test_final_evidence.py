import unittest,json
from quantv2.final_evidence import LAB_EXPORT_SQL,flatten_original
from quantv2.legacy_analysis import analyze,DEADLINE

class FinalEvidenceTests(unittest.TestCase):
 def test_readonly_sql_has_no_write_or_connection_secret(self):
  self.assertIn('REPEATABLE READ READ ONLY',LAB_EXPORT_SQL)
  for word in ('UPDATE','INSERT','DELETE','DROP','ALTER','PASSWORD'):self.assertNotIn(word,LAB_EXPORT_SQL)
 def test_original_event_flatten_and_final_analysis(self):
  state={'start':DEADLINE-1000,'end':DEADLINE,'status':'finished','samples':[{'time':DEADLINE,'equity':{'AI':19.99,'Buy and hold':20}}],
         'portfolios':{'AI':{'cash':19.99,'units':{},'fees':.01},'Buy and hold':{'cash':20,'units':{},'fees':0}}}
  events=[(DEADLINE-600,'trades',json.dumps({'strategy':'AI','trades':[{'side':'buy','symbol':'BTC/EUR','eur':10,'fee':.01},{'side':'sell','symbol':'BTC/EUR','eur':10,'fee':0}]}))]
  result=analyze(flatten_original(state,events));self.assertEqual(result['portfolios']['AI']['fill_count'],2);self.assertEqual(float(result['portfolios']['AI']['ledger_cash_mismatch']),0);self.assertIsNone(result['portfolios']['AI']['realized_pnl']);self.assertFalse(result['profitability_validated'])
 def test_wrong_deadline_and_nonfinal_never_analyzed_as_final(self):
  for state in ({'end':DEADLINE,'status':'running'},{'end':DEADLINE+1,'status':'finished'}):
   with self.assertRaises(ValueError):analyze({'kind':'lab','state':state})
