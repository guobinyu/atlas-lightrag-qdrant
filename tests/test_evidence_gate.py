import asyncio
from copy import deepcopy
import json

import pytest

from lab.ontology import verification as v
from lab.ontology.profile import uri
from lab.ontology.service import SemanticService
from lab.ontology.validation import parse_response
from evidence_helpers import checked, verdicts

MODEL = {'model': 'fixed-evidence-test'}
TEXT = '【合成】张甲在甲村参加了一次茶会。'
PAYLOAD = {'entities': [{'id': 'a', 'label': '本次茶会', 'type': 'E7', 'concrete': True},
                        {'id': 'b', 'label': '张甲', 'type': 'E21'}],
           'statements': [{'subject': 'a', 'predicate': 'P14', 'object': 'b', 'quote': TEXT}], 'deferred': []}


def setup(tmp_path):
    service = SemanticService(tmp_path)
    source = {'id': 'a', 'text': TEXT}
    bid = service.create_batch([source], MODEL, 'culture')
    return service, bid, source


@pytest.mark.parametrize('verdict', ['unsupported', 'uncertain'])
def test_quote_exists_but_relation_not_supported_never_publishes(tmp_path, verdict):
    service, bid, _ = setup(tmp_path)
    class Model:
        async def extract_semantic(self, *args):
            return json.dumps(PAYLOAD)
        async def verify_semantic(self, text, claims):
            return verdicts(claims, verdict)
    assert asyncio.run(service.run(bid, Model(), lambda *a: None)) == 'success'
    candidate = service.list_candidates()[0]
    assert candidate['evidence']['match_method'] == 'exact'
    assert candidate['verification']['verdict'] == verdict
    assert not candidate['validation']['valid']
    assert service.auto_publish()['published'] == 0
    with pytest.raises(ValueError):
        service.approve([candidate['id']], confirmed_types=True)
    assert not service.get_published_graph()['statements']


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'unknown', 'invalid-json', 'invalid-verdict', 'timeout'])
def test_check_failure_keeps_candidates_and_retry_reuses_extraction(tmp_path, kind):
    service, bid, source = setup(tmp_path)
    class Model:
        extracts = 0
        checks = 0
        fixed = False
        async def extract_semantic(self, *args):
            self.extracts += 1
            return json.dumps(PAYLOAD)
        async def verify_semantic(self, text, claims):
            self.checks += 1
            if self.fixed:
                return verdicts(claims)
            value = json.loads(verdicts(claims))
            if kind == 'missing': value = []
            if kind == 'duplicate': value *= 2
            if kind == 'unknown': value[0]['id'] = 'unknown'
            if kind == 'invalid-verdict': value[0]['verdict'] = 'yes'
            if kind == 'invalid-json': return 'not JSON'
            if kind == 'timeout': raise TimeoutError('SECRET_PROVIDER_BODY')
            return json.dumps(value)
    model = Model()
    assert asyncio.run(service.run(bid, model, lambda *a: None)) == 'failed'
    ids = [s['id'] for s in service.list_candidates()]
    assert len(ids) == 1
    assert not service.get_published_graph()['statements']
    assert not SemanticService(tmp_path).auto_publish()['published']
    assert 'SECRET_PROVIDER_BODY' not in service.store.path.read_bytes().decode(errors='ignore')
    model.fixed = True
    service.prepare_retry(bid, [source], MODEL)
    assert asyncio.run(service.run(bid, model, lambda *a: None)) == 'success'
    assert model.extracts == 1 and model.checks == 2
    assert [s['id'] for s in service.list_candidates()] == ids
    assert len(service.get_published_graph()['statements']) == 1
    service.auto_publish()
    assert len(service.list_candidates()[0]['publications']) == 1
    assert 'verification' in service.export('json')


def test_mapping_and_reversed_relation_are_checked_locally(tmp_path):
    value = parse_response(json.dumps(PAYLOAD))
    assert value['statements'][0]['predicate'] == str(uri('P14'))
    reversed_payload = deepcopy(PAYLOAD)
    reversed_payload['statements'][0].update(subject='b', object='a')
    assert v.requests(parse_response(json.dumps(reversed_payload)), TEXT) == []
    unknown = deepcopy(PAYLOAD)
    unknown['statements'][0]['predicate'] = 'P999'
    assert v.requests(parse_response(json.dumps(unknown)), TEXT) == []


def test_missing_verification_cannot_publish_through_any_entry(tmp_path):
    service, bid, _ = setup(tmp_path)
    service.save_response(service.report(bid)[0]['id'], json.dumps(PAYLOAD))
    candidate = service.list_candidates()[0]
    assert candidate['status'] == 'pending'
    assert not service.validate()[candidate['id']]['valid']
    assert service.auto_publish()['published'] == 0
    assert SemanticService(tmp_path).auto_publish()['published'] == 0
    with pytest.raises(ValueError):
        service.approve([candidate['id']], confirmed_types=True)


def test_historical_pending_requires_reextraction_and_published_is_preserved(tmp_path):
    service, bid, _ = setup(tmp_path)
    service.save_response(service.report(bid)[0]['id'], json.dumps(PAYLOAD), verification=checked(PAYLOAD, TEXT, MODEL))
    published = service.get_published_graph()['statements'][0]
    bid2 = service.create_batch([{'id': 'b', 'text': TEXT}], MODEL, 'culture')
    service.save_response(service.report(bid2)[0]['id'], json.dumps(PAYLOAD), auto_publish=False,
                          verification=checked(PAYLOAD, TEXT, MODEL))
    with service.store.connect() as db:
        for row in db.execute('SELECT id,data FROM candidates').fetchall():
            data = json.loads(row['data'])
            data.pop('verification')
            data['versions']['prompt'] = 'culture-extraction-v1'
            db.execute('UPDATE candidates SET data=? WHERE id=?', (json.dumps(data), row['id']))
    restored = SemanticService(tmp_path)
    assert restored.auto_publish() == {'published': 0, 'blocked': 1}
    assert restored.get_published_graph()['statements'][0]['id'] == published['id']


def test_modified_entity_label_invalidates_check(tmp_path):
    service, bid, _ = setup(tmp_path)
    service.save_response(service.report(bid)[0]['id'], json.dumps(PAYLOAD), auto_publish=False,
                          verification=checked(PAYLOAD, TEXT, MODEL))
    with service.store.connect() as db:
        row = db.execute('SELECT id,data FROM candidates').fetchone()
        data = json.loads(row['data'])
        data['entities']['b']['label'] = '李乙'
        db.execute('UPDATE candidates SET data=? WHERE id=?', (json.dumps(data), row['id']))
    assert service.auto_publish()['published'] == 0
