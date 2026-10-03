from urllib.parse import quote
from clients.http import HTTPClient, APIError

class BitcoinClient(HTTPClient):
    def __init__(self, base_url='https://blockstream.info/api', **kwargs):
        super().__init__(base_url, **kwargs)

    def get_tx(self, txid):
        result = self.request('/tx/' + quote(txid, safe=''))
        if not isinstance(result, dict) or result.get('txid') != txid or not isinstance(result.get('vin'), list) or not isinstance(result.get('vout'), list):
            raise APIError('Invalid Esplora transaction response')
        return result

    def get_outspends(self, txid):
        result = self.request('/tx/' + quote(txid, safe='') + '/outspends')
        if not isinstance(result, list):
            raise APIError('Invalid outspends response')
        return result

    def get_address_txs(self, address):
        result = self.request('/address/' + quote(address, safe='') + '/txs')
        if not isinstance(result, list):
            raise APIError('Invalid address transaction response')
        return result

    def iter_address_txs(self, address, max_pages=20):
        page = self.get_address_txs(address)
        seen = set()
        for _ in range(max_pages):
            for tx in page:
                if tx['txid'] not in seen:
                    seen.add(tx['txid'])
                    yield tx
            confirmed = [tx for tx in page if tx.get('status', {}).get('confirmed')]
            if len(confirmed) < 25:
                return
            page = self.request('/address/' + quote(address, safe='') + '/txs/chain/' + confirmed[-1]['txid'])
        raise APIError('Address history page limit reached; narrow the starting transaction')
