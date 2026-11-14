"""Strict decoder for Gemma's compact tool argument objects; never executes code."""
import json
import re


def loads(source):
    position = 0
    marker = '<|"|>'

    def whitespace():
        nonlocal position
        while position < len(source) and source[position].isspace():
            position += 1

    def value(depth=0):
        nonlocal position
        if depth > 64:
            raise ValueError('Tool arguments nested too deeply')
        whitespace()
        if source.startswith(marker, position):
            start = position + len(marker)
            stop = source.find(marker, start)
            if stop < 0:
                raise ValueError('Unterminated Gemma string')
            position = stop + len(marker)
            return source[start:stop]
        if position < len(source) and source[position] in '{[':
            opening = source[position]
            closing = '}' if opening == '{' else ']'
            result = {} if opening == '{' else []
            position += 1
            whitespace()
            if source[position:position + 1] == closing:
                position += 1
                return result
            while True:
                if opening == '{':
                    whitespace()
                    key = re.match(r'[A-Za-z_][\w.-]*', source[position:])
                    if key:
                        key = key.group()
                        position += len(key)
                    else:
                        key = value(depth + 1)
                    whitespace()
                    if not isinstance(key, str) or source[position:position + 1] != ':' or key in result:
                        raise ValueError('Invalid or duplicate Gemma object key')
                    position += 1
                    result[key] = value(depth + 1)
                else:
                    result.append(value(depth + 1))
                whitespace()
                separator = source[position:position + 1]
                position += 1
                if separator == closing:
                    return result
                if separator != ',':
                    raise ValueError('Invalid Gemma object separator')
        parsed, end = json.JSONDecoder().raw_decode(source[position:])
        position += end
        return parsed

    result = value()
    whitespace()
    if position != len(source) or not isinstance(result, dict):
        raise ValueError('Expected one complete Gemma argument object')
    return result
