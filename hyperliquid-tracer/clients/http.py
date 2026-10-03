import json
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

class APIError(RuntimeError):
    pass

class HTTPClient:
    def __init__(self, base_url, timeout=15, retries=2):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.retries = retries
        self.cache = {}

    def request(self, path, payload=None):
        key = (path, json.dumps(payload, sort_keys=True) if payload else None)
        if key in self.cache:
            return self.cache[key]
        for attempt in range(self.retries + 1):
            try:
                data = json.dumps(payload).encode() if payload is not None else None
                req = Request(self.base_url + path, data=data, headers={'User-Agent': 'BAY-UTXO-Tracer/1.0', 'Content-Type': 'application/json'})
                with urlopen(req, timeout=self.timeout) as response:
                    result = json.load(response)
                self.cache[key] = result
                return result
            except (URLError, TimeoutError, ValueError, OSError) as exc:
                if isinstance(exc, HTTPError) and exc.code < 500 and exc.code != 429:
                    raise APIError(f'HTTP {exc.code}: {path}') from exc
                if attempt == self.retries:
                    raise APIError(f'{path}: {exc}') from exc
                time.sleep(0.5 * (2 ** attempt))
