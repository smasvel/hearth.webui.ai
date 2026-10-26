"""Live FLM contract checks. Returned tool calls are inspected, never executed."""
import argparse
import json
import re
from pathlib import Path
import time

import requests

try:
    from .opencode_scenarios import run_workflows
except ImportError:  # Direct CLI invocation.
    from opencode_scenarios import run_workflows

def function(name, properties, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': required if required is not None else list(properties)}}}


WEATHER = function('get_weather', {'city': {'type': 'string'}})


def decode(response, stream):
    response.raise_for_status()
    if stream:
        # SSE is UTF-8 regardless of requests' ISO-8859-1 fallback for text/*.
        # splitlines also splits legal JSON characters such as U+0085/U+2028.
        content = response.content
        source = content.decode('utf-8') if isinstance(content, bytes) else response.text
        assert 'data: [DONE]' in source, 'Missing SSE terminator'
        events = [json.loads(line[6:]) for line in source.split('\n')
                  if line.startswith('data: ') and line != 'data: [DONE]']
    else:
        events = [response.json()]
    calls, text, finishes, usage = {}, [], [], {}
    for event in events:
        usage.update(event.get('usage') or {})
        for choice in event.get('choices', []):
            message = choice.get('delta', choice.get('message', {}))
            text.append(message.get('content') or '')
            if choice.get('finish_reason'):
                finishes.append(choice['finish_reason'])
            for index, call in enumerate(message.get('tool_calls') or []):
                target = calls.setdefault(call.get('index', index), {'name': '', 'arguments': '', 'id': ''})
                fn = call.get('function') or {}
                target['name'] += fn.get('name') or ''
                target['arguments'] += fn.get('arguments') or ''
                target['id'] = call.get('id') or target['id']
    for call in calls.values():
        assert call['id'], 'Missing tool call ID'
        call['arguments'] = json.loads(call['arguments'])
    return list(calls.values()), ''.join(text), finishes, usage


class Cancelled(Exception):
    pass


def reports_passed_tests(text, expected):
    """Accept common phrasings of a verified count without accepting a wrong count."""
    # Models commonly emphasize the count in Markdown (for example, "all **4**
    # tests passed"). Normalize that formatting before checking the assertion.
    text = re.sub(r'\*{1,2}(\d+)\*{1,2}', r'\1', text)
    number_words = {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'ten': '10',
    }
    text = re.sub(r'\b(?:' + '|'.join(number_words) + r')\b',
                  lambda match: number_words[match.group(0).lower()], text, flags=re.I)
    patterns = (
        r'\b(\d+)\s+(?:tests?\s+)?pass(?:ed|ing)\b',
        r'\bpassed\s+(\d+)\s+tests?\b',
        # Also accept common summaries such as "All tests passed: 4 tests, exit code 0".
        r'\btests?\s+passed\b[^0-9]{0,20}(\d+)\s+tests?\b',
    )
    matches = [match for pattern in patterns for match in re.finditer(pattern, text, re.I)]
    return bool(matches) and not re.search(r'\b[1-9]\d*\s+(?:tests?\s+)?fail(?:ed|ing)\b', text, re.I) and all(
        int(match.group(1)) == expected
        and not re.search(r'\b(?:not|never|no)\s+(?:all\s+)?$', text[:match.start()], re.I)
        for match in matches)


def run_suite(model, url, *, output=None, suite='all', context=32768, headers=None,
              on_result=None, on_case=None, cancel=None, only=None, guarded=True, profile=None):
    args = argparse.Namespace(model=model, url=url, output=output, suite=suite)
    results = []
    unavailable = None

    def check(name, prompt, stream=False, expected=None, contains=None, **extra):
        nonlocal unavailable
        if cancel and cancel.is_set():
            raise Cancelled()
        workflow_case = extra.pop('_workflow', False)
        passed_tests = extra.pop('passed_tests', None)
        metadata = {key: extra.pop(key) for key in ('workflow', 'step', 'blocked_by') if key in extra}
        if only is not None and not workflow_case and (name, stream) not in only:
            return
        if on_case:
            on_case({'name': name, 'stream': stream, 'started_at': time.time()})
        body = {'model': args.model, 'temperature': 0, 'max_tokens': 512, 'stream': stream,
                'messages': [{'role': 'user', 'content': prompt}]}
        body.update(extra)
        if body.get('tools'):
            body.setdefault('enable_thinking', False)
            body.setdefault('chat_template_kwargs', {'enable_thinking': False})
        if profile:
            body = profile(body)
        started = time.monotonic()
        result = {'name': name, 'stream': stream, **metadata}
        calls, text = [], ''
        try:
            if metadata.get('blocked_by'):
                result['skipped'] = True
                raise AssertionError('Dependent step skipped after ' + metadata['blocked_by'])
            if unavailable:
                result.update(skipped=True, blocked_by=unavailable)
                raise AssertionError('Backend unavailable after ' + unavailable)
            # CPU prefill of the large-code fixture can exceed five minutes.
            read_timeout = 900 if name == 'large-code-input' else 300
            response = requests.post(args.url, json=body, headers=headers, timeout=(10, read_timeout))
            calls, text, finishes, usage = decode(response, stream)
            result.update(usage=usage, finish_reasons=finishes)
            if expected is not None:
                observed = [(c['name'], c['arguments']) for c in calls]
                assert json.dumps(observed, sort_keys=True) == json.dumps(expected, sort_keys=True), (
                    f'Tool call mismatch. Expected: {json.dumps(expected)}. '
                    f'Received: {json.dumps(observed)}. Text: {text[:300]!r}')
                valid_finishes = {'tool_calls'} if expected else {'stop'}
                # vLLM uses stop for named choices, even when the validated response
                # contains a real tool call. Auto/required still require tool_calls.
                if expected and isinstance(body.get('tool_choice'), dict):
                    valid_finishes.add('stop')
                assert finishes and finishes[-1] in valid_finishes, (
                    f'Unexpected finish reason {finishes}; reply budget={body.get("max_tokens")}; '
                    f'completion tokens={usage.get("completion_tokens")}; text={text[:300]!r}')
            if contains:
                assert contains.lower() in text.lower(), text[:500]
            if passed_tests is not None:
                assert reports_passed_tests(text, passed_tests), f'Expected {passed_tests} passing tests; reply: {text[:500]}'
            result.update(passed=True, usage=usage)
        except Exception as exc:
            if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                unavailable = name
                result['backend_unavailable'] = True
            detail = str(exc)
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                detail += ': ' + exc.response.text[:1200]
            result.update(passed=False, error=detail[:1500])
        result['seconds'] = round(time.monotonic() - started, 2)
        results.append(result)
        print(('PASS' if result['passed'] else 'FAIL'), name, stream, result['seconds'],
              result.get('error', ''), flush=True)
        if on_result:
            on_result(result)
        if args.output:
            Path(args.output).write_text(json.dumps({'model': args.model, 'results': results}, indent=2))
        return result, calls, text

    if args.suite in ('core', 'all'):
        for stream in (False, True):
            check('plain-chat', 'What is 2+2? Reply with just the number.', stream, expected=[], contains='4')
            for choice in ('auto', 'required', {'type': 'function', 'function': {'name': 'get_weather'}}):
                check('tool-choice-' + str(choice), 'Use get_weather for Boston.', stream,
                      expected=[('get_weather', {'city': 'Boston'})], tools=[WEATHER], tool_choice=choice)
            check('tool-choice-none', 'What is 2+2? Answer directly.', stream,
                  expected=[], contains='4', tools=[WEATHER], tool_choice='none')
            check('no-tool-needed', 'What is 2+2? Answer directly.', stream,
                  expected=[], contains='4', tools=[WEATHER])
            check('parallel', 'In this one response emit TWO get_weather tool calls: Boston first, then Paris. Do not wait for the first result.', stream,
                  expected=[('get_weather', {'city': 'Boston'}), ('get_weather', {'city': 'Paris'})],
                  tools=[WEATHER], parallel_tool_calls=True)
            check('tool-result', '', stream, expected=[], contains='21', tools=[WEATHER], messages=[
                {'role': 'user', 'content': 'Weather in Boston?'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
                 'function': {'name': 'get_weather', 'arguments': '{"city":"Boston"}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"temperature":21,"conditions":"sunny"}'}])
            typed = function('save_settings', {'count': {'type': 'integer'}, 'enabled': {'type': 'boolean'},
                             'tags': {'type': 'array', 'items': {'type': 'string'}},
                             'config': {'type': 'object', 'properties': {'mode': {'type': 'string'}}}})
            check('typed-arguments', 'Call save_settings with count=3, enabled=true, tags=["code"], '
                  'config={"mode":"fast"}.', stream, tools=[typed], tool_choice='required',
                  expected=[('save_settings', {'count': 3, 'enabled': True, 'tags': ['code'], 'config': {'mode': 'fast'}})])
            check('no-arguments', 'Call ping now.', stream, tools=[function('ping', {})],
                  expected=[('ping', {})], tool_choice='required')
            check('file-read-contract', 'Call read for src/invoice.py.', stream,
                  tools=[function('read', {'path': {'type': 'string'}})],
                  expected=[('read', {'path': 'src/invoice.py'})])
            check('file-edit-contract', 'src/invoice.py contains: return price + tax_rate. '
                  'Tax is a fraction of price. Use edit to replace exactly return price + tax_rate '
                  'with return price + price * tax_rate in src/invoice.py.', stream,
                  tools=[function('edit', {k: {'type': 'string'} for k in ('path', 'oldString', 'newString')})],
                  expected=[('edit', {'path': 'src/invoice.py', 'oldString': 'return price + tax_rate',
                                      'newString': 'return price + price * tax_rate'})])
            code = '\n'.join(f'def helper_{i}(value):\n    return value + {i}\n' for i in range(min(1400, max(16, (context - 2048) // 19))))
            code += '\ndef sentinel_code():\n    return "HEARTH_CONTEXT_OK_5829"\n'
            check('large-code-input', 'Read this Python module and return only the exact string returned '
                  'by sentinel_code.\n' + code, stream, expected=[], contains='HEARTH_CONTEXT_OK_5829', max_tokens=1024,
                  enable_thinking=False, chat_template_kwargs={'enable_thinking': False})
    if args.suite in ('opencode', 'all'):
        for stream in (False, True):
            run_workflows(check, stream, only)
    if args.suite in ('extra', 'all'):
        for stream in (False, True):
            check('forced-tool-among-alternatives', 'Get the weather in Boston.', stream,
                  expected=[('get_weather', {'city': 'Boston'})],
                  tools=[function('get_time', {'timezone': {'type': 'string'}}), WEATHER],
                  tool_choice={'type': 'function', 'function': {'name': 'get_weather'}})
            check('none-when-tool-requested', 'Use get_weather for Boston.', stream,
                  expected=[], tools=[WEATHER], tool_choice='none')
            check('tool-error-followup', '', stream, expected=[], contains='unavailable', tools=[WEATHER], messages=[
                {'role': 'user', 'content': 'Get Boston weather. If the tool fails say weather unavailable; do not guess.'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'type': 'function',
                 'function': {'name': 'get_weather', 'arguments': '{"city":"Boston"}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"error":"service unavailable"}'}])
            if guarded:
                check('blocked-shell-stops', 'Call bash with command exactly: cat > /tmp/demo.txt <<EOF\nx\nEOF',
                  stream, expected=[], contains='Hearth blocked',
                  tools=[function('bash', {'command': {'type': 'string'}}),
                         function('write', {'filePath': {'type': 'string'}, 'content': {'type': 'string'}})],
                  tool_choice={'type': 'function', 'function': {'name': 'bash'}})
        if cancel and cancel.is_set():
            raise Cancelled()
        if only is not None and ('context-overflow', False) not in only:
            return results
        if on_case:
            on_case({'name': 'context-overflow', 'stream': False, 'started_at': time.time()})
        started = time.monotonic()
        try:
            if unavailable:
                raise AssertionError('Backend unavailable after ' + unavailable)
            response = requests.post(args.url, json={'model': args.model, 'max_tokens': 8,
                'messages': [{'role': 'user', 'content': 'x ' * (context + 512)}]}, headers=headers, timeout=(10, 60))
            assert response.status_code == 400, (response.status_code, response.text[:500])
            results.append({'name': 'context-overflow', 'passed': True, 'seconds': round(time.monotonic()-started, 2)})
            print('PASS context-overflow', flush=True)
        except Exception as exc:
            results.append({'name': 'context-overflow', 'passed': False, 'error': str(exc)})
            if unavailable:
                results[-1].update(skipped=True, blocked_by=unavailable)
            print('FAIL context-overflow', str(exc), flush=True)
        if on_result:
            on_result(results[-1])
        if args.output:
            Path(args.output).write_text(json.dumps({'model': args.model, 'results': results}, indent=2))
    passed = sum(r['passed'] for r in results)
    print(f'{passed}/{len(results)} live checks passed; report: {args.output}', flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--suite', choices=('core', 'extra', 'opencode', 'all'), default='all')
    parser.add_argument('--url', default='http://127.0.0.1:52626/v1/chat/completions')
    parser.add_argument('--output', default='/tmp/flm-model-results.json')
    parser.add_argument('--context', type=int, default=32768)
    args = parser.parse_args()
    results = run_suite(args.model, args.url, output=args.output, suite=args.suite, context=args.context)
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
