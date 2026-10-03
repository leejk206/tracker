import os
from pathlib import Path

def load_env(path='.env'):
    if Path(path).exists():
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))
load_env()
ESPLORA_BASE_URL = os.getenv('ESPLORA_BASE_URL', 'https://blockstream.info/api')
HYPERUNIT_BASE_URL = os.getenv('HYPERUNIT_BASE_URL', 'https://api.hyperunit.xyz')
ETH_RPC_URL = os.getenv('ETH_RPC_URL', '')
MAX_DEPTH = 7
MAX_INPUTS = 30
MAX_OUTPUTS = 30
MAX_TRANSACTIONS = 500
