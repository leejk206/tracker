import json
from pathlib import Path
cases = [('4695e2121fa84fd1d7ca41a8f481774bdf383e231cf290f56c6ac734bc018a83', '7a8b75339e92a254caf181884875c9b76d763bcda25795ba9159e25d5d14b123', '0xC18d9Dea73f7D8E8D4F9a5fAD69849F7319C3553'), ('bc1ql4u94klk265lnfur2ujk9p6uh52f2a8jhf6f37', '685ea58a17e8fa27fe99acc59e41dd4b011024dc6304f12c68a9785734256a2f', '0x3A37880ff9EbD7B45376Ef15bDA69Cefc0016575')]
for start, source, account in cases:
    data = json.loads((Path('output/traces') / (start + '.json')).read_text())
    match = next(m for m in data['matches'] if m['btc_source_tx'] == source)
    assert match['hyperliquid_account'] == account
    assert match['protocol_match_confirmed']
    assert not data['errors']
    if start.startswith('4695'):
        assert match['amount_sats'] == 499997500 and match['confidence'] == 'confirmed'
    else:
        path = ['11d2e00c757c7be8f13b88369fbbefaf052b440428e8fc079da3be2ae5c8ef2f', '8b200199bce68f97225690104574faaa864a58160559561a9fba34444baa54e3', '8b864a23729a6f94a45c2cb38e11a96e723954295820c187f1e1a7dd97b8ca2e', source]
        links = {(e['source_outpoint'].split(':')[0], e['spending_txid']) for e in data['edges']}
        assert all(pair in links for pair in zip(path, path[1:]))
        assert match['confidence'] == 'estimated'
    print('PASS', start, 'depth', match['depth'], match['confidence'])
