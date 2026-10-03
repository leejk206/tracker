import os
import unittest
from clients.bitcoin import BitcoinClient
from clients.hyperunit import HyperUnitClient
from tracing.btc_tracer import BTCTracer
import config

LIVE = os.getenv('RUN_LIVE_TESTS') == '1'

@unittest.skipUnless(LIVE, 'Set RUN_LIVE_TESTS=1 for public API integration tests')
class LiveCases(unittest.TestCase):
    def setUp(self):
        self.tracer = BTCTracer(BitcoinClient(config.ESPLORA_BASE_URL), HyperUnitClient(config.HYPERUNIT_BASE_URL))

    def test_case1(self):
        result = self.tracer.trace_btc_tx('4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83')
        match = next(m for m in result.matches if m['btc_source_tx'] == '7a8b75339e92a254caf181884875c9b76d763bcda25795ba9159e25d5d14b123')
        self.assertEqual(match['hyperliquid_account'].lower(), '0xc18d9dea73f7d8e8d4f9a5fad69849f7319c3553')
        self.assertEqual(match['amount_sats'], 499997500)
        self.assertEqual(match['confidence'], 'confirmed')
        self.assertFalse(result.errors)

    def test_case2(self):
        result = self.tracer.trace_address('bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37')
        match = next(m for m in result.matches if m['btc_source_tx'] == '685ea58a17e8fa27fe99acc59e41dd4b011024dc6304f12c68a9785734256a2f')
        self.assertEqual(match['hyperliquid_account'].lower(), '0x3a37880ff9ebd7b45376ef15bda69cefc0016575')
        self.assertTrue(match['protocol_match_confirmed'])
        expected = ['11d2e00c757c7be8f13b88369fbbefaf052b440428e8fc079da3be2ae5c8ef2f', '8b200199bce68f97225690104574faaa864a58160559561a9fba34444baa54e3', '8b864a23729a6f94a45c2cb38e11a96e723954295820c187f1e1a7dd97b8ca2e', match['btc_source_tx']]
        links = {(e['source_outpoint'].split(':')[0], e['spending_txid']) for e in result.edges}
        for pair in zip(expected, expected[1:]):
            self.assertIn(pair, links)
        self.assertFalse(result.errors)
