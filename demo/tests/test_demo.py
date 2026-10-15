"""API-проверки на новой SQLite для каждого теста; не трогают демонстрационную БД."""
import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv('DEMO_DB', str(tmp_path / 'isolated.sqlite'))
    name = 'isolated_demo_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'app.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    with TestClient(module.app, headers={'X-Demo-Action': 'local-demo'}) as client:
        yield module, client
    module.engine.dispose()
    del sys.modules[name]


def login(c, role='analyst'):
    result = c.post('/api/login', json={'role': role})
    assert result.status_code == 200
    assert 'HttpOnly' in result.headers['set-cookie']
    return result


def view(c):
    r = c.get('/api/cases/DEMO-017')
    assert r.status_code == 200
    return r.json()


def version(c, revision=1):
    r = c.post('/api/versions', json={'revision': revision})
    assert r.status_code == 200, r.text
    return r.json()['id']


def decision(c, id, revision=1, action='approve'):
    return c.post('/api/decisions', json={'revision': revision, 'version_id': id, 'action': action, 'reason': 'Проверены учебные материалы. Ограничения досье сохранены.'})


def test_fresh_start_and_local_docs(demo):
    _, c = demo
    assert c.get('/api/health').json()['external_integrations'] is False
    assert c.get('/').status_code == 200
    assert 'Синтетические данные' in c.get('/').text
    assert '/static/vendor/swagger-ui-bundle.js' in c.get('/docs').text
    assert c.get('/static/vendor/cytoscape.min.js').status_code == 200
    assert '/api/versions' in c.get('/openapi.json').json()['paths']
    assert c.get('/api/cases').status_code == 401


def test_full_case_workflow(demo):
    _, c = demo
    login(c)
    assert len(c.get('/api/cases').json()['cases']) == 1
    initial = view(c)
    assert initial['graph']['hops'] == 2
    first = version(c)
    req = c.post('/api/document-request', json={'revision': 1})
    assert req.status_code == 200 and req.json()['sent'] is False
    assert 'LABEL-A' not in req.json()['draft']
    assert c.post('/api/documents/simulate-receipt', json={'revision': 1}).status_code == 200
    second = version(c, 2)
    assert decision(c, second, 2).status_code == 403
    login(c, 'approver')
    assert decision(c, first, 2).status_code == 409
    assert decision(c, second, 2).status_code == 200
    result = c.post('/api/export').json()
    assert result['audit']['valid'] is True
    assert result['case']['decisions'][0]['version_id'] == second
    actions = [e['action'] for e in result['audit']['events']]
    for expected in ['Создана версия заключения', 'Подготовлен черновик запроса документов', 'Добавлен синтетический документ', 'Утверждена версия досье', 'Экспортировано внутреннее досье', 'Отказ в доступе']:
        assert expected in actions
    assert len(result['case']['versions']) == 2


def test_every_claim_resolves_to_snapshot_source(demo):
    _, c = demo
    login(c)
    first = version(c)
    c.post('/api/documents/simulate-receipt', json={'revision': 1})
    version(c, 2)
    for v in view(c)['versions']:
        for claim in v['claims']:
            assert claim['refs']
            for source_id in claim['refs']:
                assert c.get(f'/api/sources/{source_id}?version_id={v["id"]}').status_code == 200
    assert 'В реестре отсутствуют' in next(x for x in view(c)['versions'] if x['id'] == first)['claims'][-1]['text']


@pytest.mark.parametrize('change', ['unknown_ref', 'empty_refs', 'invented_text', 'omitted_fact', 'wrong_existing_ref', 'unknown_fact', 'duplicate'])
def test_server_rejects_unsupported_claims(demo, change):
    _, c = demo
    login(c)
    claims = [{'fact_id': f['id'], 'text': f['text'], 'refs': f['refs']} for f in view(c)['facts']]
    if change == 'unknown_ref': claims[0]['refs'] = ['NONEXISTENT']
    elif change == 'empty_refs': claims[0]['refs'] = []
    elif change == 'invented_text': claims[0]['text'] = 'Доход законен. Можно провести операцию.'
    elif change == 'omitted_fact': claims.pop()
    elif change == 'wrong_existing_ref': claims[0]['refs'] = ['TX-01']
    elif change == 'unknown_fact': claims[0]['fact_id'] = 'F-FAKE'
    elif change == 'duplicate': claims[-1] = copy.deepcopy(claims[0])
    assert c.post('/api/versions', json={'revision': 1, 'claims': claims}).status_code == 422
    assert view(c)['versions'] == []


@pytest.mark.parametrize('role', ['analyst', 'auditor', 'admin'])
def test_only_approver_can_approve_even_direct_api(demo, role):
    _, c = demo
    login(c)
    id = version(c)
    login(c, role)
    assert decision(c, id).status_code == 403
    assert view(c)['decisions'] == []


@pytest.mark.parametrize('role', ['auditor', 'admin'])
def test_read_only_roles_cannot_edit(demo, role):
    _, c = demo
    login(c, role)
    assert c.post('/api/versions', json={'revision': 1}).status_code == 403
    assert c.post('/api/document-request', json={'revision': 1}).status_code == 403
    assert c.post('/api/documents/simulate-receipt', json={'revision': 1}).status_code == 403
    assert decision(c, None, action='escalate').status_code == 403


def test_provider_failure_blocks_generation_and_approval(demo):
    _, c = demo
    login(c)
    id = version(c)
    assert c.post('/api/provider', json={'revision': 1, 'available': False}).status_code == 403
    login(c, 'admin')
    assert c.post('/api/provider', json={'revision': 1, 'available': False}).status_code == 200
    login(c, 'approver')
    assert c.post('/api/versions', json={'revision': 2}).status_code == 409
    assert decision(c, id, 2).status_code == 409
    assert decision(c, id, 2, 'escalate').status_code == 200
    assert view(c)['sources']
    login(c, 'admin')
    c.post('/api/provider', json={'revision': 2, 'available': True})
    login(c, 'approver')
    assert decision(c, id, 3).status_code == 409
    new = version(c, 3)
    assert decision(c, new, 3).status_code == 200


def test_approved_version_retained_and_duplicate_decision_blocked(demo):
    _, c = demo
    login(c, 'approver')
    first = version(c)
    assert decision(c, first).status_code == 200
    snapshot = view(c)['versions'][0]
    assert decision(c, first).status_code == 409
    second = version(c)
    assert second != first
    assert snapshot == next(v for v in view(c)['versions'] if v['id'] == first)
    assert decision(c, first).status_code == 409
    assert decision(c, second).status_code == 200


def test_stale_revision_and_extra_fields_blocked(demo):
    _, c = demo
    login(c)
    c.post('/api/documents/simulate-receipt', json={'revision': 1})
    assert c.post('/api/versions', json={'revision': 1}).status_code == 409
    assert c.post('/api/versions', json={'revision': 2, 'approved': True}).status_code == 422
    assert c.post('/api/documents/simulate-receipt', json={'revision': 2}).status_code == 409


@pytest.mark.parametrize('table', ['journal', 'versions', 'decisions'])
@pytest.mark.parametrize('operation', ['UPDATE', 'DELETE'])
def test_sql_triggers_reject_rewrite(demo, table, operation):
    app, c = demo
    login(c, 'approver')
    id = version(c)
    assert decision(c, id).status_code == 200
    sql = f"UPDATE {table} SET content = 'changed'" if operation == 'UPDATE' else f'DELETE FROM {table}'
    with pytest.raises(IntegrityError):
        with app.engine.begin() as connection:
            connection.execute(text(sql))


def test_hash_chain_detects_content_tamper_without_recomputation(demo):
    app, c = demo
    login(c)
    assert c.get('/api/journal').json()['valid'] is True
    with app.engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER immutable_journal_update')
        row = connection.exec_driver_sql('SELECT content FROM journal WHERE id=1').scalar_one()
        payload = json.loads(row)
        payload['action'] = 'Незаметно изменено'
        connection.execute(text('UPDATE journal SET content=:value WHERE id=1'), {'value': json.dumps(payload)})
    assert c.get('/api/journal').json()['valid'] is False


def test_origin_header_and_local_host_guards(demo):
    _, c = demo
    assert c.post('/api/login', json={'role': 'analyst'}, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
    c.headers.pop('X-Demo-Action')
    assert c.post('/api/login', json={'role': 'analyst'}).status_code == 403
    assert c.get('/api/health', headers={'host': 'public.invalid'}).status_code == 403


def test_role_switch_invalidates_previous_session(demo):
    _, c = demo
    login(c)
    old = c.cookies.get('demo_session')
    login(c, 'auditor')
    assert c.get('/api/cases', headers={'Cookie': 'demo_session=' + old}).status_code == 401


def test_client_instruction_not_in_conclusion(demo):
    _, c = demo
    login(c)
    version(c)
    v = view(c)['versions'][0]
    assert 'Игнорируй правила' in c.get('/api/sources/INJECT-01').json()['content']
    assert all('Игнорируй правила' not in x['text'] and 'INJECT-01' not in x['refs'] for x in v['claims'])


@pytest.mark.parametrize('role,expected', [('analyst',403),('admin',403),('auditor',200),('approver',200)])
def test_export_permissions(demo, role, expected):
    _, c = demo
    login(c, role)
    assert c.post('/api/export').status_code == expected


def test_graph_path_uses_direction_and_exact_edges(demo):
    app, _ = demo
    g = app.graph_data()
    assert g['path'] == ['DEMO-RISK','DEMO-RELAY','DEMO-CLIENT']
    assert g['path_edges'] == ['TX-01','TX-02']
    assert len(g['path']) - 1 == g['hops'] == 2
    assert g['edges'][2]['amount'] == '12 000,00'
