from collections import deque
from clients.http import APIError
from models.trace_result import TraceResult, btc
from tracing.unit_matcher import match_operations
from config import MAX_INPUTS, MAX_OUTPUTS, MAX_TRANSACTIONS

class BTCTracer:
    def __init__(self, bitcoin, hyperunit, max_inputs=MAX_INPUTS, max_outputs=MAX_OUTPUTS, max_transactions=MAX_TRANSACTIONS):
        self.bitcoin, self.hyperunit = bitcoin, hyperunit
        self.max_inputs, self.max_outputs, self.max_transactions = max_inputs, max_outputs, max_transactions

    def trace_btc_tx(self, txid, max_depth=7):
        return self._trace(txid, [(txid, None)], max_depth)

    def trace_address(self, address, max_depth=7):
        # Only outputs paid to the starting address seed the graph.
        seeds = []
        for tx in self.bitcoin.iter_address_txs(address):
            indexes = [i for i, out in enumerate(tx['vout']) if out.get('scriptpubkey_address') == address]
            if indexes:
                seeds.append((tx['txid'], indexes))
        return self._trace(address, seeds, max_depth)

    def _trace(self, start, seeds, max_depth):
        if max_depth < 0:
            raise ValueError('max_depth must be nonnegative')
        result = TraceResult(start)
        queue = deque((txid, 0, None, indexes, False) for txid, indexes in seeds)
        visited, outpoints, match_ids = set(), set(), set()
        while queue:
            txid, depth, parent, indexes, inherited_mixed = queue.popleft()
            if txid in visited:
                result.stops.append({'txid': txid, 'depth': depth, 'reason': 'already_visited', 'parent_outpoint': parent})
                continue
            if len(visited) >= self.max_transactions:
                result.stops.append({'txid': txid, 'depth': depth, 'reason': 'max_transactions'})
                continue
            visited.add(txid)
            try:
                tx = self.bitcoin.get_tx(txid)
                if parent and not any(f"{i.get('txid')}:{i.get('vout')}" == parent for i in tx['vin']):
                    raise APIError('Outspend does not correspond to a spending input')
                reason = 'high_fanin' if len(tx['vin']) > self.max_inputs else ('high_fanout' if len(tx['vout']) > self.max_outputs else None)
                if reason:
                    result.stops.append({'txid': txid, 'depth': depth, 'reason': reason, 'parent_outpoint': parent})
                    continue
                mixed = inherited_mixed or (parent is not None and len(tx['vin']) > 1)
                spend_error = None
                try:
                    spends = self.bitcoin.get_outspends(txid)
                    if len(spends) != len(tx['vout']):
                        raise APIError('Outspends count differs from outputs')
                except APIError as exc:
                    spends = [None] * len(tx['vout'])
                    spend_error = str(exc)
                    result.errors.append({'txid': txid, 'stage': 'outspends', 'error': spend_error})
                for i in indexes if indexes is not None else range(len(tx['vout'])):
                    out = tx['vout'][i]
                    point = f'{txid}:{i}'
                    if point in outpoints:
                        continue
                    outpoints.add(point)
                    spend = spends[i]
                    node = {'outpoint': point, 'depth': depth, 'txid': txid, 'vout': i, 'address': out.get('scriptpubkey_address'), 'value_sats': out['value'], 'value_btc': btc(out['value']), 'spent': spend.get('spent') if spend else None, 'spending_txid': spend.get('txid') if spend else None, 'parent_txid': parent.split(':')[0] if parent else None, 'parent_outpoint': parent, 'attribution_uncertain': mixed, 'hyperunit_matches': [], 'stop_reason': None}
                    result.nodes.append(node)
                    if node['address']:
                        try:
                            matches = match_operations(tx, i, depth, self.hyperunit.get_operations(node['address']), mixed)
                            node['hyperunit_matches'] = [m['match_id'] for m in matches]
                            for match in matches:
                                if match['match_id'] not in match_ids:
                                    match_ids.add(match['match_id'])
                                    result.matches.append(match)
                        except APIError as exc:
                            result.errors.append({'outpoint': point, 'stage': 'hyperunit', 'error': str(exc)})
                    if node['hyperunit_matches']:
                        node['stop_reason'] = 'unit_found'
                    elif spend_error:
                        node['stop_reason'] = 'api_error'
                    elif not node['spent']:
                        node['stop_reason'] = 'unspent'
                    elif depth >= max_depth:
                        node['stop_reason'] = 'max_depth'
                    else:
                        target = node['spending_txid']
                        if not target:
                            node['stop_reason'] = 'api_error'
                            continue
                        result.edges.append({'source_outpoint': point, 'spending_txid': target, 'spending_vin': spend.get('vin')})
                        queue.append((target, depth + 1, point, None, mixed))
            except (APIError, KeyError, TypeError, ValueError) as exc:
                result.stops.append({'txid': txid, 'depth': depth, 'reason': 'api_error', 'parent_outpoint': parent})
                result.errors.append({'txid': txid, 'stage': 'transaction', 'error': str(exc)})
        return result
