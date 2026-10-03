import re
from models.trace_result import btc

def match_operations(tx, vout, depth, operations, mixed=False):
    output = tx['vout'][vout]
    address = output.get('scriptpubkey_address')
    matches = []
    for op in operations:
        source = str(op.get('sourceTxHash', ''))
        parts = source.split(':')
        if parts[0].lower() != tx['txid'].lower():
            continue
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != str(vout)):
            continue
        if op.get('protocolAddress') != address or op.get('sourceChain') != 'bitcoin':
            continue
        account = op.get('destinationAddress')
        valid = op.get('destinationChain') == 'hyperliquid' and bool(re.fullmatch(r'0x[0-9a-fA-F]{40}', str(account)))
        amount_ok = str(op.get('sourceAmount', '')) == str(output['value'])
        completed = valid and op.get('state') == 'done' and amount_ok
        confidence = ('estimated' if mixed else 'confirmed') if completed else 'unresolved'
        senders = sorted({i.get('prevout', {}).get('scriptpubkey_address') for i in tx['vin'] if i.get('prevout', {}).get('scriptpubkey_address')})
        matches.append({'match_id': f"{tx['txid']}:{vout}:{op.get('operationId', source)}", 'btc_source_tx': tx['txid'], 'vout': vout, 'btc_sender_address': senders[0] if len(senders) == 1 else None, 'btc_sender_addresses': senders, 'btc_protocol_address': address, 'amount_sats': output['value'], 'amount_btc': btc(output['value']), 'hyperliquid_account': account if valid else None, 'hyperunit_state': op.get('state'), 'depth': depth, 'confidence': confidence, 'protocol_match_confirmed': completed, 'attribution_uncertain': mixed, 'operation': op})
    return matches
