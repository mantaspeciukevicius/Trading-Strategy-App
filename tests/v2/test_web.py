import unittest,json,copy
from unittest.mock import patch
from fastapi.testclient import TestClient
from quantv2 import web
from .helpers import history,rules
class WebTests(unittest.TestCase):
 def setUp(self):
  self.patches=[patch('quantv2.web.Path.read_text',return_value=json.dumps({'username':'fixture','password':'fixture-secret','csrf':'fixture-token'})),patch('quantv2.web.repo.list',return_value=[]),patch('quantv2.web.repo.health',return_value={'ok':True,'database':True,'worker_fresh':True}),patch('quantv2.web.worker.archive',return_value=(history(),{'BTC/EUR':rules()})),patch('quantv2.web.worker.wake.set'),patch('quantv2.web.repo.create',return_value='fixture')]
  for p in self.patches:p.start()
  self.client=TestClient(web.app)
 def tearDown(self):
  self.client.close()
  for p in reversed(self.patches):p.stop()
 def post(self,url,body,headers=None):return self.client.post(url,json=body,auth=('fixture','fixture-secret'),headers=headers or {'X-V2-CSRF':'fixture-token'})
 def test_auth_required_for_dashboard_data(self):self.assertEqual(self.client.get('/api/status').status_code,401)
 def test_csrf_required_for_writes(self):self.assertEqual(self.post('/api/check',{},headers={'X-V2-CSRF':'bad'}).status_code,403)
 def test_cross_origin_write_rejected(self):self.assertEqual(self.post('/api/check',{},headers={'X-V2-CSRF':'fixture-token','Origin':'https://evil.invalid'}).status_code,403)
 def test_no_live_order_route(self):self.assertEqual(self.post('/api/orders',{}).status_code,404)
 def test_mirror_not_independent_experiment(self):
  body={'request_id':'00000000-0000-4000-8000-000000000001','strategy':'ai_mirror','capital':'20','start':720000,'end':903600};self.assertEqual(self.post('/api/experiments',body).status_code,422)
 def test_invalid_window_rejected(self):
  body={'request_id':'00000000-0000-4000-8000-000000000001','strategy':'btc_trend','capital':'20','start':1,'end':903600};self.assertEqual(self.post('/api/experiments',body).status_code,400)
 def test_default_minimum_remains_five(self):
  body={'request_id':'00000000-0000-4000-8000-000000000001','strategy':'btc_trend','capital':'20','start':720000,'end':903600};self.assertEqual(self.post('/api/experiments',body).status_code,200)
  args=web.repo.create.call_args[0][0];self.assertEqual(args['costs']['practical_min'],'5')
 def test_security_headers(self):
  response=self.client.get('/health');self.assertEqual(response.headers['X-Frame-Options'],'DENY');self.assertIn('Content-Security-Policy',response.headers)
 def test_operational_endpoint_requires_authentication(self):self.assertEqual(self.client.get('/api/operations').status_code,401)
 def test_health_refuses_operational_failure(self):
  with patch('quantv2.monitoring.operational_status',return_value={'failures':['backup_failed']}):
   response=self.client.get('/health');self.assertEqual(response.status_code,503);self.assertIn('backup_failed',response.json()['operational_failures'])
