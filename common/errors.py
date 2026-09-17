"""
Error rendering helpers shared across apps.

Lives in the top-level ``common`` package rather than in any one app because
two independent layers now need the same behaviour: telemetry promotion and
the offline sync queue both have to turn a DRF ``ValidationError`` into a
single readable sentence, and both hand that sentence to a field client.
Duplicating it would let the two drift, and a client that parses one shape and
receives another is a bug that only shows up offline.
"""


def describe_drf_error(exc) -> str:
    """Render a DRF ValidationError as one readable line.

    Field errors arrive as ``{'readings': [{...}], 'depth_m': ['...']}`` and a
    mobile client needs a sentence, not a nested structure — this is shown to
    an inspector standing on site. Nested per-item errors (the ``readings``
    list, where each entry is itself a field-error dict) are flattened and
    numbered, so the operator can tell *which* reading was rejected.
    """
    detail = getattr(exc, 'detail', exc)

    def render(errors):
        if isinstance(errors, dict):
            return ' | '.join(f'{field}: {render(value)}'
                              for field, value in errors.items())
        if isinstance(errors, (list, tuple)):
            parts = []
            for index, item in enumerate(errors):
                text = render(item)
                # A list of per-item dicts is a collection of rows; number
                # them. A list of plain messages is one row's message set.
                parts.append(f'#{index + 1} {text}'
                             if isinstance(item, dict) else text)
            return '; '.join(parts)
        return str(errors)

    return render(detail)
