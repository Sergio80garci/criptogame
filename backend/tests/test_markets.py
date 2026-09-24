import unittest
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from app import buda
from app.main import app

def ticker(m):
    return {'ticker': {'market_id': m, 'last_price': ['100.5', 'CLP'],
        'min_ask': ['101', 'CLP'], 'max_bid': ['100', 'CLP'],
        'volume': ['0.001', m.split('-')[0]], 'price_variation_24h': '-0.02',
        'price_variation_7d': '0'}}

class MarketsTests(unittest.TestCase):
    def request(self, handler):
        with patch.object(buda, 'get_http_client', lambda: httpx.AsyncClient(
            base_url=buda.BUDA_API_BASE, transport=httpx.MockTransport(handler))):
            return TestClient(app).get('/api/markets')

    def test_success(self):
        def handler(r):
            self.assertEqual(r.method, 'GET')
            self.assertTrue(r.url.path.startswith('/api/v2/markets/'))
            self.assertNotIn('X-SBTC-APIKEY', r.headers)
            return httpx.Response(200, json=ticker(r.url.path.split('/')[-2]))
        response = self.request(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['markets']), 7)
        self.assertEqual(response.json()['markets'][0]['last_price'], 100.5)
        self.assertEqual(response.json()['markets'][0]['price_variation_24h'], -0.02)

    def test_partial_failure(self):
        def handler(r):
            m = r.url.path.split('/')[-2]
            return httpx.Response(404) if m == 'SOL-CLP' else httpx.Response(200, json=ticker(m))
        response = self.request(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['markets']), 6)
        self.assertEqual(response.json()['errors'][0]['code'], 'unavailable')

    def test_timeout(self):
        def handler(r):
            raise httpx.ReadTimeout('internal diagnostic', request=r)
        response = self.request(handler)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(response.json()['errors']), 7)
        self.assertNotIn('internal diagnostic', response.text)

    def test_invalid_values(self):
        for value in ['NaN', 'Infinity', '-1', True]:
            with self.subTest(value=value):
                def handler(r):
                    data = ticker(r.url.path.split('/')[-2])
                    data['ticker']['last_price'][0] = value
                    return httpx.Response(200, json=data)
                self.assertEqual(self.request(handler).status_code, 502)

    def test_currency(self):
        with self.assertRaises(ValueError):
            buda.amount(['10', 'USD'], 'CLP')

    def test_local_routes(self):
        client = TestClient(app)
        self.assertEqual(client.get('/health').status_code, 200)
        self.assertEqual(client.get('/openapi.json').status_code, 200)
        self.assertIn('Actualizar', client.get('/').text)
