"""Prompt budgeting for the local FLM runtime; never discard conversation turns."""
from functools import lru_cache
from pathlib import Path

import psutil
from jinja2 import Environment, TemplateError

DEFAULT_CONTEXT = 8192
RESERVE = 256
MODEL_CONTEXTS = {
    'qwen3-it:4b': 32768,  # Installed FLM catalog default; larger windows need validation.
    'minicpm-v-4.7:1b': 262144,
    'gemma4-it:e4b': 131072,
    'gemma4e-flash:e4b': 1024,
    'llama3.2:1b': 131072,
    'qwen3.8-mtp:27b': 8192,  # Larger v1.0.8 allocations hit attn_layout MAX_L_LIMIT.
}


def supported_context(model):
    # FLM v1.0.8 documents 256K for MiniCPM. Qwen's installed NPU kernel has a
    # separate padding/layout limit: do not copy MiniCPM's setting to that model.
    return MODEL_CONTEXTS.get(str(model).removeprefix('flm/'), DEFAULT_CONTEXT)


def output_limit(model=None, limit=None):
    """Advertise a reply budget that leaves at least half the window for input."""
    limit = context_limit(model) if limit is None else limit
    tag = str(model or '').removeprefix('flm/')
    cap = 4096 if tag.startswith('qwen3.8') else 2048
    return max(1, min(cap, limit // 2))


def _model_aware_service():
    try:
        return 'flm_serve.py' in Path('/etc/systemd/system/flm.service').read_text()
    except OSError:
        return False


def context_limit(model=None):
    aware = _model_aware_service()
    try:
        active = (Path.home() / '.config/fastflowlm/active').read_text().strip()
    except OSError:
        active = ''
    for process in psutil.process_iter(['cmdline']):
        try:
            args = process.info['cmdline'] or []
            if args and Path(args[0]).name == 'flm' and 'serve' in args and '--ctx-len' in args:
                value = int(args[args.index('--ctx-len') + 1])
                if value > 0:
                    if model and aware and active and str(model).removeprefix('flm/') != active:
                        return supported_context(model)
                    return value
        except (psutil.Error, ValueError, IndexError):
            continue
    return supported_context(model) if model and aware else DEFAULT_CONTEXT


def compact_tools(body):
    """Drop documentation annotations, retaining descriptions and validation constraints."""
    def schema(value):
        if not isinstance(value, dict):
            return value
        out = {}
        for key, item in value.items():
            if key in {'title', 'examples', '$comment'}:
                continue
            if key in {'properties', '$defs', 'definitions', 'patternProperties', 'dependentSchemas'} and isinstance(item, dict):
                item = {name: schema(child) for name, child in item.items()}
            elif key in {'allOf', 'anyOf', 'oneOf', 'prefixItems'} and isinstance(item, list):
                item = [schema(child) for child in item]
            elif key in {'items', 'additionalProperties', 'not', 'if', 'then', 'else', 'contains', 'propertyNames', 'unevaluatedProperties'}:
                item = schema(item)
            out[key] = item
        return out
    tools = []
    for tool in body.get('tools') or []:
        fn = tool.get('function')
        if not isinstance(fn, dict):
            tools.append(tool)
            continue
        tools.append({**tool, 'function': {**fn, 'parameters': schema(fn.get('parameters', {}))}})
    return {**body, 'tools': tools} if tools else body


@lru_cache(maxsize=4)
def _model_assets(directory, tokenizer_mtime, template_mtime):
    from tokenizers import Tokenizer
    root = Path(directory)
    tokenizer = Tokenizer.from_file(str(root / 'tokenizer.json'))
    env = Environment()
    def fail(message):
        raise ValueError(message)
    env.globals['raise_exception'] = fail
    return tokenizer, env.from_string((root / 'chat_template.jinja').read_text())


def prompt_tokens(body):
    """Count text with the installed model's template/tokenizer; unknown/media inputs skip."""
    model = str(body.get('model', '')).removeprefix('flm/')
    if model.startswith('qwen3.8'):
        directory = 'Qwen3.8-27B-NPU2'
    elif model == 'minicpm-v-4.7:1b':
        directory = 'MiniCPM-V-4.7-1B-NPU2'
    else:
        return None
    if any(not isinstance(m.get('content', ''), (str, type(None))) for m in body.get('messages', [])):
        return None  # vision/audio tokens need runtime-side accounting
    root = Path.home() / '.config/flm/models' / directory
    try:
        tokenizer, template = _model_assets(str(root), (root / 'tokenizer.json').stat().st_mtime_ns,
                                            (root / 'chat_template.jinja').stat().st_mtime_ns)
        rendered = template.render(**body, add_generation_prompt=True)
        return len(tokenizer.encode(rendered, add_special_tokens=False).ids)
    except (OSError, ValueError, TypeError, ImportError, TemplateError):
        return None


def prepare(body):
    body = compact_tools(body)
    limit = context_limit(body.get('model'))
    count = prompt_tokens(body)
    cap = output_limit(body.get('model'), limit)
    requested = body.get('max_tokens', body.get('max_completion_tokens', cap))
    # Small requested replies can use more input space; keep template padding.
    minimum_reply = min(256, requested) if type(requested) is int and requested > 0 else 256
    if count is not None and count + RESERVE + minimum_reply > limit:
        return body, overflow_error(limit, count)
    remaining = limit - RESERVE - (count or 0)
    if isinstance(requested, int) and requested > 0:
        key = 'max_completion_tokens' if 'max_completion_tokens' in body and 'max_tokens' not in body else 'max_tokens'
        body = {**body, key: min(requested, cap, max(1, remaining))}
    return body, None


def overflow_error(limit=None, count=None):
    limit = limit or context_limit()
    size = f' The local template/tokenizer counts {count} input tokens.' if count is not None else ''
    return {'error': {
        'message': f'FLM context capacity is {limit} tokens.{size} '
                   'The prompt must leave room for the reply. In OpenCode select the '
                   'flm-small agent, reduce enabled MCP tools/project instructions, or '
                   'compact/start a new session. Compaction cannot shrink fixed system/tool overhead.',
        'type': 'invalid_request_error', 'code': 'context_length_exceeded',
        'param': 'messages',
    }}
