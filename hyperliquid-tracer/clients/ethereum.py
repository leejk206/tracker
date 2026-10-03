from clients.http import HTTPClient, APIError

class EthereumClient(HTTPClient):
    def __init__(self, rpc_url=None, **kwargs):
        if rpc_url is None:
            from config import ETH_RPC_URL
            rpc_url = ETH_RPC_URL
        if not rpc_url:
            raise ValueError('Set ETH_RPC_URL in .env')
        super().__init__(rpc_url, **kwargs)

    def _rpc(self, method, tx_hash):
        result = self.request('', {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': [tx_hash]})
        if not isinstance(result, dict) or 'error' in result or 'result' not in result:
            raise APIError('Ethereum JSON-RPC error')
        return result['result']

    def get_eth_tx(self, tx_hash):
        return self._rpc('eth_getTransactionByHash', tx_hash)

    def get_eth_receipt(self, tx_hash):
        return self._rpc('eth_getTransactionReceipt', tx_hash)
