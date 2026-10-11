"""Conservative, auditable dates for recently discovered public topics."""
from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import re
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo('Asia/Shanghai')
DATE_CHECK_VERSION = 2
DATE_RE = re.compile(r'(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?)?')


def date_value(text):
    """Return the precision interval; date-only evidence cannot prove an exact hour."""
    match = DATE_RE.search(str(text))
    if not match:
        return None
    year, month, day, hour, minute, second, offset = match.groups()
    try:
        value = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), tzinfo=BEIJING)
        if offset:
            value = datetime.fromisoformat(f'{year}-{int(month):02}-{int(day):02}T{int(hour or 0):02}:{int(minute or 0):02}:{int(second or 0):02}{offset.replace("Z", "+00:00")}').astimezone(BEIJING)
        end = value if hour else value + timedelta(days=1) - timedelta(microseconds=1)
        return value, end
    except ValueError:
        return None


def metadata_dates(body):
    class Dates(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = []
            self.in_json = False
            self.buffer = ''

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'meta' and (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower() in {
                'article:published_time', 'datepublished', 'pubdate', 'publishdate', 'publish_date'}:
                value = attrs.get('content', '')
                if date_value(value):
                    self.values.append({'value': value, 'evidence': self.get_starttag_text()})
            if tag == 'script' and attrs.get('type') == 'application/ld+json':
                self.in_json, self.buffer = True, ''

        def handle_data(self, data):
            if self.in_json:
                self.buffer += data

        def handle_endtag(self, tag):
            if tag == 'script' and self.in_json:
                self.in_json = False
                try:
                    self.walk(json.loads(self.buffer))
                except (ValueError, TypeError):
                    pass

        def walk(self, value):
            if isinstance(value, list):
                for child in value:
                    self.walk(child)
            elif isinstance(value, dict):
                # Exclude nested unrelated entities (e.g. breadcrumb dates).
                types = value.get('@type', [])
                types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
                if any(t in ('Article', 'NewsArticle', 'BlogPosting', 'ReportageNewsArticle') for t in types):
                    date = value.get('datePublished')
                    if isinstance(date, str) and date_value(date):
                        self.values.append({'value': date, 'evidence': json.dumps({'datePublished': date}, ensure_ascii=False)})
                if '@graph' in value:
                    self.walk(value['@graph'])
    parser = Dates()
    parser.feed(body.decode('utf-8', errors='replace'))
    return parser.values[:10]


def verify_date(source, extracted, now, days):
    """Never use discovery/collection time as evidence. Event date beats publication."""
    now = datetime.fromisoformat(now).astimezone(BEIJING)
    cutoff = now - timedelta(days=days)
    evidence, publication, event = [], [], []
    invalid_evidence = False
    for item in source.get('published_metadata', []):
        parsed = date_value(item['value'])
        if parsed:
            publication.append(parsed)
            evidence.append({'kind': 'published', 'quote': item['evidence'], 'date': parsed[0].isoformat(), 'origin': 'metadata'})
    for field, dest in [('published', publication), ('event', event)]:
        item = extracted.get(field)
        # Models commonly serialize an absent date as an object of nulls.
        if isinstance(item, dict) and not item.get('date') and not item.get('quote'):
            continue
        if not isinstance(item, dict):
            if item is not None:
                invalid_evidence = True
            continue
        quote = item.get('quote', '')
        value = item.get('date', '')
        # Compare parsed date against the actual quoted date, not just quote existence.
        metadata_quote = field == 'published' and isinstance(quote, str) and bool(quote) and any(
            quote in entry.get('evidence', '') for entry in source.get('published_metadata', [])
        )
        if not isinstance(quote, str) or not quote or (quote not in source['text'] and not metadata_quote):
            invalid_evidence = True
            continue
        quoted, supplied = date_value(quote), date_value(value)
        # A day-only model value may summarize a precise timestamp in the quote.
        # Keep the original quote's precision, never accept invented precision.
        if not quoted or not supplied or not (supplied[0] <= quoted[0] and supplied[1] >= quoted[1]):
            invalid_evidence = True
            continue
        dest.append(quoted)
        evidence.append({'kind': field, 'quote': quote, 'date': quoted[0].isoformat(), 'origin': 'text'})
    chosen = event or publication
    conflict = any(values and max(v[0] for v in values) > min(v[1] for v in values) for values in (publication, event))
    result = {'status': 'unverified', 'published_at': publication[0][0].isoformat() if publication else None,
              'event_at': event[0][0].isoformat() if event else None, 'evidence': evidence,
              'published_precision': ('day' if publication[0][0] != publication[0][1] else 'time') if publication else None,
              'event_precision': ('day' if event[0][0] != event[0][1] else 'time') if event else None,
              'reason': '日期缺失或无法定位原文依据'}
    if invalid_evidence:
        result['reason'] = '提取日期未能与原文证据对应，需要核实'
    elif conflict:
        result['reason'] = '日期证据存在冲突'
    elif chosen:
        start, end = chosen[0]
        result['effective_at'] = start.isoformat()
        result['precision'] = 'day' if start != end else 'time'
        if end < cutoff or start > now:
            result.update(status='excluded', reason='事件或发布时间不在本次时间范围内')
        elif start < cutoff:
            result['reason'] = '日期精度不足，无法确认落在时间范围内'
        else:
            result.update(status='verified', reason='按事件时间核验' if event else '仅按发布时间核验；事件时间未确认')
    return result


def topic_dates(extractions, source_ids):
    dates = [extractions[s]['date'] for s in source_ids]
    event_days = {d['event_at'][:10] for d in dates if d.get('event_at')}
    if all(d['status'] == 'verified' for d in dates) and len(event_days) <= 1:
        status = 'verified'
    elif all(d['status'] == 'excluded' for d in dates):
        status = 'excluded'
    else:
        status = 'unverified'
    latest = max(dates, key=lambda d: d.get('effective_at', ''))
    return {'date_status': status, 'date': latest.get('effective_at', ''), 'date_precision': latest.get('precision')}


def snapshot_time(run):
    return run.get('snapshot_at', run['created_at'])
