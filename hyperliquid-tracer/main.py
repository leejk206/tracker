import argparse
import csv
import json
import re
from pathlib import Path
from clients.bitcoin import BitcoinClient
from clients.hyperunit import HyperUnitClient
from clients.http import APIError
from tracing.btc_tracer import BTCTracer
import config

def save(result, output=None, fmt='json'):
    data = result.to_dict()
    json_path = Path(output) if output and fmt == 'json' else Path('output/traces') / (result.start + '.json')
    csv_path = Path(output) if output and fmt == 'csv' else Path('output/matches') / (result.start + '.csv')
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ['start_tx', 'depth', 'btc_tx', 'vout', 'btc_address', 'amount_sats', 'btc_amount', 'unit_protocol_address', 'hl_account', 'unit_state', 'confidence']
    with csv_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for m in result.matches:
            writer.writerow(dict(zip(fields, [result.start, m['depth'], m['btc_source_tx'], m['vout'], m['btc_sender_address'], m['amount_sats'], m['amount_btc'], m['btc_protocol_address'], m['hyperliquid_account'], m['hyperunit_state'], m['confidence']])))
    return json_path, csv_path

def main():
    parser = argparse.ArgumentParser(description='Bitcoin UTXO -> HyperUnit -> Hyperliquid tracer')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--tx')
    group.add_argument('--address')
    parser.add_argument('--max-depth', type=int, default=config.MAX_DEPTH)
    parser.add_argument('--output')
    parser.add_argument('--format', choices=['json', 'csv'], default='json')
    args = parser.parse_args()
    if args.max_depth < 0 or (args.tx and not re.fullmatch('[0-9a-fA-F]{64}', args.tx)) or (args.address and not re.fullmatch('[a-zA-Z0-9]{14,90}', args.address)):
        parser.error('Invalid start identifier or depth')
    tracer = BTCTracer(BitcoinClient(config.ESPLORA_BASE_URL), HyperUnitClient(config.HYPERUNIT_BASE_URL))
    try:
        result = tracer.trace_btc_tx(args.tx.lower(), args.max_depth) if args.tx else tracer.trace_address(args.address, args.max_depth)
    except APIError as exc:
        parser.exit(1, f'API error: {exc}\n')
    print('[BTC TRACE]')
    for n in result.nodes:
        print(f"Depth {n['depth']} {n['outpoint']} | {n['value_btc']} BTC -> {n['address']} | {n['stop_reason'] or 'follow'}")
    print('\n[HYPERUNIT MATCH]')
    for m in result.matches:
        print(f"BTC source tx: {m['btc_source_tx']}\nAmount: {m['amount_btc']} BTC\nHyperUnit state: {m['hyperunit_state']}\nHyperliquid account: {m['hyperliquid_account']}\nConfidence: {m['confidence'].upper()}\n")
    paths = save(result, args.output, args.format)
    print('Saved:', *paths)
    print(f'{len(result.nodes)} outputs, {len(result.matches)} matches, {len(result.errors)} API errors')
    return 1 if not result.nodes else (2 if result.errors else 0)

if __name__ == '__main__':
    raise SystemExit(main())
