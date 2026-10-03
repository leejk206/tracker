import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from clients.http import HTTPClient, APIError
from clients.bitcoin import BitcoinClient
from clients.ethereum import EthereumClient
from main import save
from models.trace_result import TraceResult

class ClientTests(unittest.TestCase):
    def test_http_cache(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"operations": []}'
        with patch('clients.http.urlopen', return_value=Response()) as request:
            client = HTTPClient('https://example.com')
            self.assertEqual(client.request('/x'), client.request('/x'))
            self.assertEqual(request.call_count, 1)
    def test_invalid_bitcoin_response(self):
        client = BitcoinClient()
        with patch.object(client, 'request', return_value={}):
            with self.assertRaises(APIError): client.get_tx('a'*64)
    def test_ethereum_methods(self):
        client = EthereumClient('https://example.com')
        with patch.object(client, 'request', return_value={'result': {'hash': '0xtest'}}) as request:
            self.assertEqual(client.get_eth_tx('0xtest'), {'hash': '0xtest'})
            self.assertEqual(request.call_args.args[1]['method'], 'eth_getTransactionByHash')
            client.get_eth_receipt('0xtest')
            self.assertEqual(request.call_args.args[1]['method'], 'eth_getTransactionReceipt')
    def test_ethereum_rpc_error(self):
        client = EthereumClient('https://example.com')
        with patch.object(client, 'request', return_value={'error': {'code': -1}}):
            with self.assertRaises(APIError): client.get_eth_tx('0xtest')
