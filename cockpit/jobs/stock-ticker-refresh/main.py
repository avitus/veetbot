import math
import re
from datetime import datetime, timezone

SCHEMA = {'type': 'object', 'properties': {'observedAt': {'type': 'integer'}, 'expectedIntervalSec': {'type': 'integer'}, 'source': {'type': 'string'}, 'quotes': {'type': 'array', 'items': {'type': 'object'}}, 'error': {'type': ['string', 'null']}}, 'required': ['observedAt', 'expectedIntervalSec', 'source', 'quotes', 'error'], 'additionalProperties': False}

def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None

def base_quote(stock):
    kind = 'INDEX' if stock.get('is_index', False) else 'EQUITY'
    return {'symbol': stock['symbol'], 'name': stock['name'], 'currency': stock['currency'], 'instrumentType': kind, 'unit': 'points' if kind == 'INDEX' else 'currency'}

def normalize(stock, response, now_ms):
    base = base_quote(stock)
    if response.get('status') != 200:
        return dict(base, status='unavailable', message='Quote source unavailable')
    chart = (response.get('json') or {}).get('chart') or {}
    results = chart.get('result') or []
    if chart.get('error') or not results:
        return dict(base, status='unavailable', message='Quote not available from source')
    meta = results[0].get('meta') or {}
    identity = ' '.join(str(meta.get(k) or '') for k in ('longName', 'shortName')).lower()
    if (meta.get('symbol') != stock['symbol'] or stock['match_name'].lower() not in identity or meta.get('exchangeName') != stock['exchange'] or meta.get('currency') != stock['currency'] or meta.get('instrumentType') != base['instrumentType']):
        return dict(base, status='unavailable', message='Quote identity could not be verified')
    price = number(meta.get('regularMarketPrice'))
    stamp = number(meta.get('regularMarketTime'))
    if price is None or price <= 0 or stamp is None or stamp <= 0 or stamp * 1000 > now_ms + 300000:
        return dict(base, status='unavailable', message='Invalid price or quote timestamp')
    prior = number(meta.get('previousClose'))
    if prior is None or prior <= 0:
        prior = number(meta.get('chartPreviousClose'))
    change = price - prior if prior is not None and prior > 0 else None
    fraction = change / prior if change is not None else None
    return dict(base, status='ok', price=price, quoteAt=int(stamp * 1000), change=change, changeFraction=fraction)

def main(inputs, job):
    stocks = inputs.get('stocks') or []
    if not 1 <= len(stocks) <= 20:
        raise ValueError('Provide between 1 and 20 instruments')
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    quotes = []
    fixture = 'fixture_responses' in inputs
    for stock in stocks:
        if not re.fullmatch(r'\^?[A-Z][A-Z0-9.-]{0,14}', stock['symbol']):
            raise ValueError('Invalid symbol')
        if not isinstance(stock.get('is_index', False), bool):
            raise ValueError('is_index must be boolean')
        try:
            if fixture:
                response = inputs['fixture_responses'].get(stock['symbol'], {'status': 404})
            else:
                url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + stock['symbol'].replace('^', '%5E') + '?interval=1d&range=1d'
                response = job.http.request('GET', url)
            quote = normalize(stock, response, now_ms)
        except Exception:
            quote = dict(base_quote(stock), status='unavailable', message='Quote fetch or parsing failed')
        quotes.append(quote)
    statuses = [q['status'] for q in quotes]
    if 'expected_statuses' in inputs:
        assert statuses == inputs['expected_statuses'], 'Unexpected normalization result'
        for q in quotes:
            assert q['unit'] == ('points' if q['instrumentType'] == 'INDEX' else 'currency')
            if q['status'] == 'ok' and q['changeFraction'] is not None:
                assert abs((q['price'] - q['change']) * q['changeFraction'] - q['change']) < 0.00001
    missing = sum(s != 'ok' for s in statuses)
    data = {'observedAt': now_ms, 'expectedIntervalSec': 600, 'source': 'Yahoo Finance', 'quotes': quotes, 'error': (str(missing) + ' quote(s) unavailable') if missing else None}
    if not fixture:
        job.feed.publish('agent.stock-ticker', data, expected_interval_seconds=600, schema=SCHEMA)
    return data
