from urllib.parse import quote
from clients.http import HTTPClient, APIError

class HyperUnitClient(HTTPClient):
    def __init__(self, base_url='https://api.hyperunit.xyz', **kwargs):
        super().__init__(base_url, **kwargs)

    def get_operations(self, address):
        result = self.request('/operations/' + quote(address, safe=''))
        if not isinstance(result, dict) or not isinstance(result.get('operations'), list):
            raise APIError('Invalid HyperUnit operations response')
        return result['operations']
