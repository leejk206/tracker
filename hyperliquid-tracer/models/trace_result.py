from dataclasses import dataclass, field
from decimal import Decimal

def btc(sats):
    return format(Decimal(sats) / Decimal(100000000), '.8f')

@dataclass
class TraceResult:
    start: str
    nodes: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    matches: list = field(default_factory=list)
    stops: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self):
        accounts = {}
        for match in self.matches:
            account = match['hyperliquid_account']
            if account:
                group = accounts.setdefault(account.lower(), {'hyperliquid_account': account, 'deposits': [], 'total_sats': 0})
                group['deposits'].append(match['match_id'])
                group['total_sats'] += match['amount_sats']
        return {**self.__dict__, 'accounts': list(accounts.values()), 'attribution_note': 'UTXO reachability and protocol mapping do not prove ownership or taint; output amounts are full deposit amounts.'}
