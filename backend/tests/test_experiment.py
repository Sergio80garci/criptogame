import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx
from fastapi.testclient import TestClient
from app import experiment as ex, candles, buda
from app.main import app

ANSWER={'model':'test-model','choice':'up','probabilities':{'up':.7,'down':.2,'flat':.1},'confidence':.8}

class ExperimentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.pathpatch=patch.object(ex,'DB_PATH',Path(self.temp.name)/'test.sqlite3')
        self.pathpatch.start()
    def tearDown(self):
        self.pathpatch.stop()
        self.temp.cleanup()

    async def test_prediction_is_saved_once_before_target(self):
        with patch.object(ex.time,'time',return_value=8000), patch.object(ex.predictions,'_ensure_configured',return_value='test'), patch.object(candles,'fetch_candles',AsyncMock(return_value=[{'direction':'down'}])) as fetch, patch.object(ex,'ask',AsyncMock(return_value=dict(ANSWER))) as ask:
            first=await ex.predict()
            second=await ex.predict()
        self.assertEqual(first['target_start'],8100)
        self.assertEqual(first,second)
        ask.assert_awaited_once()
        fetch.assert_awaited_once_with(3600,7200)
        self.assertLess(first['prediction_at'],first['target_start'])
        self.assertEqual(first['context']['closed_candles'],[{'direction':'down'}])

    async def test_late_answer_is_logged_not_scored(self):
        clock=[8000]
        async def answer(state):
            clock[0]=8101
            return dict(ANSWER)
        with patch.object(ex.time,'time',side_effect=lambda:clock[0]), patch.object(ex.predictions,'_ensure_configured',return_value='test'), patch.object(candles,'fetch_candles',AsyncMock(return_value=[{'direction':'down'}])), patch.object(ex,'ask',side_effect=answer):
            row=await ex.predict()
        self.assertNotIn('prediction',row)
        self.assertIn('error',row)

    async def test_resolution_uses_target_interval_even_when_late(self):
        ex.append(8100,'prediction',{**ANSWER,'baseline':'down'})
        with patch.object(ex.time,'time',return_value=20000), patch.object(candles,'fetch_candles',AsyncMock(return_value=[{'direction':'up','open':'10','close':'11'}])) as fetch:
            await ex.resolve()
            await ex.resolve()
        fetch.assert_awaited_once_with(8100,9000)
        stats=ex.summary(ex.records())
        self.assertEqual(stats['accuracy'],1)
        self.assertEqual(stats['baseline_accuracy'],0)
        self.assertAlmostEqual(stats['brier'],.14)
        self.assertEqual(stats['calibration'][0]['count'],1)

    async def test_missing_data_retry_then_not_evaluable(self):
        ex.append(8100,'prediction',{**ANSWER,'baseline':'down'})
        with patch.object(candles,'fetch_candles',AsyncMock(side_effect=candles.IncompleteData('gap'))):
            with patch.object(ex.time,'time',return_value=10000):
                await ex.resolve()
                self.assertNotIn('result',ex.records()[0])
            with patch.object(ex.time,'time',return_value=100000):
                await ex.resolve()
        self.assertEqual(ex.summary(ex.records())['resolved'],0)
        self.assertEqual(ex.records()[0]['result']['status'],'not_evaluable')

    def test_immutable_storage_and_existing_flow_available(self):
        ex.append(8100,'attempt',{'test':True})
        with ex.connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):conn.execute('DELETE FROM events')
            with self.assertRaises(sqlite3.IntegrityError):conn.execute("UPDATE events SET payload='{}'")
        client=TestClient(app)
        self.assertEqual(client.post('/api/predictions/save',json={}).status_code,400)
        with patch('app.predictions.delete_record', AsyncMock(return_value=True)):
            self.assertEqual(client.delete('/api/predictions/tracking/1').status_code,200)
        self.assertIn('Predicción JEV', client.get('/').text)
        self.assertIn('Próxima vela', client.get('/experiment').text)

    def test_invalid_probabilities(self):
        for p in ({'up':2,'down':0,'flat':0},{'up':float('nan'),'down':.2,'flat':.1}):
            with self.assertRaises(ValueError):ex.validate({**ANSWER,'probabilities':p})

    async def test_candles_pagination_and_half_open_boundaries(self):
        seen=[]
        def handler(r):
            cursor=int(r.url.params['timestamp']);seen.append(cursor)
            rows=([[1799999,'1','12'],[900001,'1','10']] if cursor==1799999 else [[900001,'1','10'],[899999,'1','9']])
            return httpx.Response(200,json={'trades':{'entries':rows}})
        with patch.object(buda,'get_http_client',lambda:httpx.AsyncClient(base_url=buda.BUDA_API_BASE,transport=httpx.MockTransport(handler))):
            result=await candles.fetch_candles(900,1800)
        self.assertEqual(seen,[1799999,900001])
        self.assertEqual(result[0]['open'],'10')
        self.assertEqual(result[0]['close'],'12')
        self.assertEqual(result[0]['trades'],2)

    async def test_pagination_limit_never_returns_partial_candle(self):
        def handler(r):
            return httpx.Response(200,json={'trades':{'entries':[[1000000,'1','10']]}})
        with patch.object(buda,'get_http_client',lambda:httpx.AsyncClient(base_url=buda.BUDA_API_BASE,transport=httpx.MockTransport(handler))):
            with self.assertRaises(candles.IncompleteData):await candles.fetch_candles(900,1800,max_pages=1)
