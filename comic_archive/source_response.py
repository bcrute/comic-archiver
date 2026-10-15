"""Fixed response classifications; never retain response text or raw headers."""


def source_response_diagnostics(value):
    kind = value.get('source_response_kind')
    if isinstance(kind, str) and kind in {'challenge', 'html', 'json', 'text', 'other'}:
        return {'source_response_kind': kind}
    return {}
