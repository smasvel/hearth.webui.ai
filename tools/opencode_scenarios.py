"""Deterministic OpenCode conversations. Tool results and user answers are fixtures.

Nothing in this module executes generated commands or touches a target project.
Schemas mirror the OpenCode requests captured by Hearth locally.
"""
import json


def tool(name, properties, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': name,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': list(properties) if required is None else required}}}


STRING = {'type': 'string'}
QUESTION = tool('question', {'questions': {'type': 'array', 'items': {
    'type': 'object', 'properties': {
        'question': STRING, 'header': STRING,
        'options': {'type': 'array', 'items': {'type': 'object',
            'properties': {'label': STRING, 'description': STRING}, 'required': ['label', 'description']}},
        'multiple': {'type': 'boolean'}}, 'required': ['question', 'header', 'options']}}})
TOOLS = [QUESTION,
    tool('task', {'description': STRING, 'prompt': STRING, 'subagent_type': STRING, 'task_id': STRING},
         ['description', 'prompt', 'subagent_type']),
    tool('glob', {'pattern': STRING, 'path': STRING}, ['pattern']),
    tool('grep', {'pattern': STRING, 'path': STRING, 'include': STRING}, ['pattern']),
    tool('read', {'filePath': STRING, 'offset': {'type': 'integer'}, 'limit': {'type': 'integer'}}, ['filePath']),
    tool('edit', {'filePath': STRING, 'oldString': STRING, 'newString': STRING,
                  'replaceAll': {'type': 'boolean'}}, ['filePath', 'oldString', 'newString']),
    tool('write', {'filePath': STRING, 'content': STRING}),
    tool('bash', {'command': STRING, 'workdir': STRING, 'timeout': {'type': 'integer'}}, ['command']),
    tool('todowrite', {'todos': {'type': 'array', 'items': {'type': 'object',
        'properties': {'content': STRING, 'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed', 'cancelled']},
                       'priority': {'type': 'string', 'enum': ['high', 'medium', 'low']}},
        'required': ['content', 'status', 'priority']}}})]
ROOT = '/workspace/atlas'


def call_step(name, function, arguments, output, instruction=None):
    return {'name': name, 'expected': [(function, arguments)], 'outputs': [output],
            'instruction': instruction or f'Next call {function} with exactly these arguments: {json.dumps(arguments)}.'}


def question(question, header, labels, multiple=False):
    return {'questions': [{'question': question, 'header': header, 'multiple': multiple,
        'options': [{'label': label, 'description': description} for label, description in labels]}]}


def scenarios():
    todo = lambda status: {'todos': [{'content': 'Implement invoice totals and verify tests', 'status': status, 'priority': 'high'}]}
    invoice = ROOT + '/src/invoice.py'
    route = ROOT + '/src/api.py'
    build = [
        call_step('plan', 'todowrite', todo('in_progress'), 'Task list updated.'),
        call_step('discover', 'glob', {'pattern': '**/*.py', 'path': ROOT},
                  json.dumps([invoice, route, ROOT + '/tests/test_invoice.py'])),
        call_step('search', 'grep', {'pattern': 'invoice_total', 'path': ROOT, 'include': '*.py'},
                  'src/invoice.py:1:def invoice_total(price, tax_rate):\nsrc/api.py:1:from .invoice import invoice_total'),
        call_step('read-source', 'read', {'filePath': invoice},
                  'def invoice_total(price, tax_rate):\n    return price + tax_rate\n'),
        call_step('read-caller', 'read', {'filePath': route},
                  'from .invoice import invoice_total\ndef total(price):\n    return invoice_total(price, 0)\n'),
        call_step('edit-source', 'edit', {'filePath': invoice, 'oldString': 'return price + tax_rate',
                  'newString': 'return price + price * tax_rate'}, 'Edit successful.'),
        call_step('edit-caller', 'edit', {'filePath': route, 'oldString': 'return invoice_total(price, 0)',
                  'newString': 'return invoice_total(price, 0.1)'}, 'Edit successful.'),
        call_step('write-test', 'write', {'filePath': ROOT + '/tests/test_tax.py',
                  'content': 'from src.invoice import invoice_total\n\ndef test_tax():\n    assert invoice_total(100, 0.1) == 110\n'}, 'File created.'),
        call_step('test-failure', 'bash', {'command': 'pytest -q', 'workdir': ROOT},
                  'FAILED tests/test_invoice.py::test_negative - expected 0, got -11. 1 failed, 3 passed. Exit code: 1'),
        call_step('reread-after-failure', 'read', {'filePath': invoice},
                  'def invoice_total(price, tax_rate):\n    return price + price * tax_rate\n'),
        call_step('fix-failure', 'edit', {'filePath': invoice, 'oldString': 'return price + price * tax_rate',
                  'newString': 'return max(0, price + price * tax_rate)'}, 'Edit successful.'),
        call_step('verify', 'bash', {'command': 'pytest -q', 'workdir': ROOT}, '4 passed. Exit code: 0'),
        call_step('complete-plan', 'todowrite', todo('completed'), 'Task list updated.'),
        {'name': 'finish', 'instruction': 'Summarize the verified result. State how many tests passed. Do not call more tools.',
         'expected': [], 'passed_tests': 4},
    ]
    variants = [
        ('single-choice', question('Which database?', 'Database', [('SQLite (Recommended)', 'Embedded database'), ('PostgreSQL', 'Database server')]),
         [['PostgreSQL']], 'PostgreSQL'),
        ('multiple-choice', question('Which features?', 'Features', [('API', 'HTTP endpoints'), ('CLI', 'Terminal interface'), ('UI', 'Browser interface')], True),
         [['API', 'CLI']], 'API and CLI'),
        ('custom-answer', question('Which package name?', 'Package', [('atlas', 'Default package'), ('demo', 'Example package')]),
         [['atlas_enterprise']], 'atlas_enterprise'),
        ('batched-questions', {'questions': [
            question('Which database?', 'Database', [('SQLite', 'Embedded'), ('PostgreSQL', 'Server')])['questions'][0],
            question('Which interface?', 'Interface', [('CLI', 'Terminal'), ('API', 'HTTP')])['questions'][0]]},
         [['SQLite'], ['API']], 'SQLite and API'),
    ]
    workflows = [('build', build)]
    for name, args, answers, selected in variants:
        workflows.append((name, [
            call_step('ask', 'question', args, {'answers': answers}),
            call_step('resume', 'todowrite', {'todos': [{'content': 'Implement ' + selected,
                'status': 'in_progress', 'priority': 'high'}]}, 'Task list updated.',
                'Use the returned user answer. Do not ask again. Next call todowrite with exactly: ' +
                json.dumps({'todos': [{'content': 'Implement ' + selected, 'status': 'in_progress', 'priority': 'high'}]})),
        ]))
    rejected = question('Install optional analytics?', 'Analytics', [('Skip (Recommended)', 'Continue without it'), ('Install', 'Add analytics')])
    workflows.append(('dismissed-question', [
        call_step('ask', 'question', rejected, {'error': 'User dismissed this question. Continue without optional analytics.'}),
        {'name': 'resume', 'instruction': 'The user dismissed optional analytics. Reply "Skipping optional analytics" and do not ask again or install anything.',
         'expected': [], 'contains': 'Skipping optional analytics'},
    ]))
    workflows.append(('edit-recovery', [
        call_step('read', 'read', {'filePath': invoice}, 'def invoice_total(price, tax_rate):\n    return price + price * tax_rate\n'),
        call_step('failed-edit', 'edit', {'filePath': invoice, 'oldString': 'return price + tax_rate', 'newString': 'return 0'},
                  'Error: oldString not found in content. Re-read the file.'),
        call_step('reread', 'read', {'filePath': invoice}, 'def invoice_total(price, tax_rate):\n    return price + price * tax_rate\n'),
        call_step('retry', 'edit', {'filePath': invoice, 'oldString': 'return price + price * tax_rate',
                  'newString': 'return max(0, price + price * tax_rate)'}, 'Edit successful.'),
    ]))
    workflows.append(('parallel-read', [
        {'name': 'read-both', 'instruction': 'Read both files in one response, invoice first then api. '
            'Call read with filePath /workspace/atlas/src/invoice.py and then read with filePath /workspace/atlas/src/api.py. Emit both <tool_call> blocks in this same response.',
         'expected': [('read', {'filePath': invoice}), ('read', {'filePath': route})],
         'outputs': ['def invoice_total(price, tax_rate): return price + price * tax_rate',
                     'from .invoice import invoice_total']},
        {'name': 'resume', 'instruction': 'Both reads completed. Reply "Both files inspected" without calling tools again.',
         'expected': [], 'contains': 'Both files inspected'},
    ]))
    workflows.append(('permission-denied', [
        call_step('request', 'bash', {'command': 'npm install optional-metrics', 'workdir': ROOT},
            {'error': 'Permission denied by user. Do not retry installation or use another tool to bypass this decision.'}),
        {'name': 'respect-denial', 'instruction': 'Installation was denied. Reply "Continuing without optional-metrics". '
            'Do not retry, ask again, or install through another tool.', 'expected': [], 'contains': 'Continuing without optional-metrics'},
    ]))
    workflows.append(('task-result', [
        call_step('delegate', 'task', {'description': 'Inspect invoice tests',
            'prompt': 'Read tests/test_invoice.py and report the test command. Do not edit files.', 'subagent_type': 'explore'},
            {'task_id': 'task_fixture_41', 'result': 'Use pytest -q. The test suite has four invoice tests.'}),
        call_step('resume', 'bash', {'command': 'pytest -q', 'workdir': ROOT}, '4 passed. Exit code: 0'),
    ]))
    workflows.append(('paged-read', [
        call_step('first-page', 'read', {'filePath': invoice, 'offset': 1, 'limit': 20},
            'Lines 1-20 of 40. Continue at offset 21. No invoice_total definition in this page.'),
        call_step('next-page', 'read', {'filePath': invoice, 'offset': 21, 'limit': 20},
            '21: def invoice_total(price, tax_rate):\n22:     return price + tax_rate'),
        call_step('edit-without-line-numbers', 'edit', {'filePath': invoice,
            'oldString': 'return price + tax_rate', 'newString': 'return price + price * tax_rate'}, 'Edit successful.'),
    ]))
    workflows.append(('checkpoint-resume', [
        call_step('verify-pending-work', 'bash', {'command': 'pytest -q', 'workdir': ROOT},
            '4 passed. Exit code: 0',
            'Session checkpoint after context compaction: project /workspace/atlas; invoice totals and API edits '
            'are finished; the user already selected PostgreSQL; all implementation todos are complete except '
            'verification. Tests have NOT run yet. Resume from this checkpoint without repeating questions '
            'or edits. Next call bash with command pytest -q and workdir /workspace/atlas.'),
        {'name': 'finish', 'instruction': 'Report the verified result and how many tests passed, without more tool calls.',
         'expected': [], 'passed_tests': 4},
    ]))
    return workflows


def run_workflows(check, stream, only=None):
    """Replay real validated call IDs/results; stop dependent requests on failure."""
    for workflow, steps in scenarios():
        prefix = 'opencode-' + workflow + '-'
        if only is not None and not any(name.startswith(prefix) and mode == stream for name, mode in only):
            continue
        messages = [{'role': 'system', 'content':
            'You are testing an OpenCode coding workflow in /workspace/atlas. '
            'Follow the next requested step using the offered tools. Tool results and user answers are authoritative. '
            'Never repeat an answered question, invent tool results, or declare success before verification.'},
            {'role': 'user', 'content': 'Implement invoice totals across the project, add tests, recover from failures, and finish after verification.'}]
        blocked = None
        for index, step in enumerate(steps, 1):
            name = prefix + step['name']
            messages.append({'role': 'user', 'content': step['instruction']})
            outcome = check(name, '', stream, expected=step['expected'], contains=step.get('contains'),
                passed_tests=step.get('passed_tests'),
                messages=messages, tools=TOOLS, max_tokens=1024,
                workflow=workflow, step=index, blocked_by=blocked, _workflow=True)
            if outcome is None:
                continue
            result, calls, text = outcome
            if not result['passed']:
                blocked = blocked or name
                continue
            if calls:
                messages.append({'role': 'assistant', 'content': text or None, 'tool_calls': [
                    {'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': json.dumps(c['arguments'])}}
                    for c in calls]})
                for call, output in zip(calls, step['outputs']):
                    messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                     'content': output if isinstance(output, str) else json.dumps(output)})
            else:
                messages.append({'role': 'assistant', 'content': text})
