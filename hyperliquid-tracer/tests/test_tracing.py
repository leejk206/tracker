import unittest
from clients.http import APIError
from tracing.btc_tracer import BTCTracer
from tracing.unit_matcher import match_operations

A, B, C = 'a'*64, 'b'*64, 'c'*64
ADDRESS = 'bc1ptest'
ACCOUNT = '0x' + '1'*40

def tx(txid, inputs, outputs):
    return {'txid': txid, 'vin': [{'txid': t, 'vout': v, 'prevout': {'scriptpubkey_address': 'sender'}} for t, v in inputs], 'vout': [{'value': amount, 'scriptpubkey_address': address} for amount, address in outputs]}

def operation(txid=B, index=0):
    return {'sourceTxHash': f'{txid}:{index}', 'protocolAddress': ADDRESS, 'sourceChain': 'bitcoin', 'destinationChain': 'hyperliquid', 'destinationAddress': ACCOUNT, 'sourceAmount': '100', 'state': 'done'}

class FakeBitcoin:
    def __init__(self, mixed=False):
        self.txs = {A: tx(A, [(C, 0)], [(200, 'start')]), B: tx(B, [(A, 0)] + ([(C, 1)] if mixed else []), [(100, ADDRESS), (90, 'change')])}
    def get_tx(self, key):
        return self.txs[key]
    def get_outspends(self, key):
        return [{'spent': True, 'txid': B, 'vin': 0}] if key == A else [{'spent': False}, {'spent': False}]

class FakeUnit:
    def get_operations(self, address):
        return [operation()] if address == ADDRESS else []

class TracerTests(unittest.TestCase):
    def test_verified_path(self):
        result = BTCTracer(FakeBitcoin(), FakeUnit()).trace_btc_tx(A)
        self.assertEqual(result.matches[0]['confidence'], 'confirmed')
        self.assertEqual(result.matches[0]['amount_sats'], 100)
        self.assertEqual(result.nodes[1]['stop_reason'], 'unit_found')
        self.assertEqual(result.edges[0]['source_outpoint'], A+':0')
    def test_merge_is_estimated(self):
        result = BTCTracer(FakeBitcoin(True), FakeUnit()).trace_btc_tx(A)
        self.assertEqual(result.matches[0]['confidence'], 'estimated')
        self.assertTrue(result.matches[0]['protocol_match_confirmed'])
    def test_wrong_tx_or_output_rejected(self):
        t = FakeBitcoin().txs[B]
        self.assertEqual(match_operations(t, 0, 1, [operation(A)]), [])
        self.assertEqual(match_operations(t, 0, 1, [operation(B, 1)]), [])
    def test_wrong_chain_and_amount(self):
        t = FakeBitcoin().txs[B]
        op = operation()
        op['destinationChain'] = 'ethereum'
        self.assertEqual(match_operations(t, 0, 1, [op])[0]['confidence'], 'unresolved')
        op = operation()
        op['sourceAmount'] = '101'
        self.assertEqual(match_operations(t, 0, 1, [op])[0]['confidence'], 'unresolved')
    def test_depth(self):
        result = BTCTracer(FakeBitcoin(), FakeUnit()).trace_btc_tx(A, 0)
        self.assertEqual(len(result.nodes), 1)
        self.assertEqual(result.nodes[0]['stop_reason'], 'max_depth')
    def test_fanin(self):
        result = BTCTracer(FakeBitcoin(True), FakeUnit(), max_inputs=1).trace_btc_tx(A)
        self.assertEqual(result.stops[-1]['reason'], 'high_fanin')
    def test_unit_failure_does_not_stop_btc(self):
        class BrokenUnit:
            def get_operations(self, address):
                raise APIError('offline')
        result = BTCTracer(FakeBitcoin(), BrokenUnit()).trace_btc_tx(A)
        self.assertEqual(len(result.nodes), 3)
        self.assertEqual(len(result.errors), 3)
    def test_invalid_spend_link(self):
        bitcoin = FakeBitcoin()
        bitcoin.txs[B]['vin'][0]['txid'] = C
        result = BTCTracer(bitcoin, FakeUnit()).trace_btc_tx(A)
        self.assertEqual(result.matches, [])
        self.assertEqual(result.stops[-1]['reason'], 'api_error')

if __name__ == '__main__':
    unittest.main()
