import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import httpx
from app import predictions, buda

class TrackingResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_historical_deadline_not_current_ticker(self):
        records=[{'id':1,'market_id':'BTC-CLP','resolved':False,
            'resolves_at':'2020-01-01T00:00:00+00:00','price_at_prediction':100,
            'predicted_direction':'up'}]
        target=int(datetime(2020,1,1,tzinfo=timezone.utc).timestamp()*1000)
        def handler(request):
            self.assertTrue(request.url.path.endswith('/trades'))
            self.assertEqual(int(request.url.params['timestamp']),target)
            return httpx.Response(200,json={'trades':{'entries':[[str(target-1000),'1','110','buy']]}})
        with patch.object(predictions,'_load_tracking',return_value=records), patch.object(predictions,'_save_tracking') as save, patch.object(buda,'get_http_client',lambda:httpx.AsyncClient(base_url=buda.BUDA_API_BASE,transport=httpx.MockTransport(handler))):
            self.assertEqual(await predictions.resolve_due_tracking(),1)
            self.assertTrue(save.call_args.args[0][0]['correct'])
            self.assertEqual(save.call_args.args[0][0]['price_at_resolution'],110)

    async def test_no_historical_trade_remains_pending(self):
        records=[{'id':1,'market_id':'BTC-CLP','resolved':False,
            'resolves_at':'2020-01-01T00:00:00+00:00','price_at_prediction':100,
            'predicted_direction':'up'}]
        with patch.object(predictions,'_load_tracking',return_value=records), patch.object(predictions,'_save_tracking') as save, patch.object(buda,'get_http_client',lambda:httpx.AsyncClient(base_url=buda.BUDA_API_BASE,transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'trades':{'entries':[]}})))):
            self.assertEqual(await predictions.resolve_due_tracking(),0)
            save.assert_not_called()
