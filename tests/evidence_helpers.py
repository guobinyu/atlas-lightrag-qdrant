"""Explicitly synthetic supported verdicts for storage and workflow tests."""
import json
from lab.ontology import verification as v
from lab.ontology.validation import parse_response


def verdicts(claims, verdict='supported'):
    return json.dumps([{'id': c['id'], 'verdict': verdict, 'reason': '固定测试判断，非模型质量证明'} for c in claims])


def checked(payload, text, model):
    value = parse_response(json.dumps(payload))
    claims = v.requests(value, text)
    return v.parse(verdicts(claims), claims, text, model)


def verification_reply(messages):
    content = messages[-1]['content']
    if '\n陈述：' not in content:
        return None
    claims = json.loads(content.split('\n陈述：', 1)[1].split('\n原文：', 1)[0])
    return verdicts(claims)
