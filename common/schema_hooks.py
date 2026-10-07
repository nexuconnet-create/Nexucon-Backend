"""
Schema post-processing hooks.

One hook, and it exists for one reason: this platform registers some routes
under two spellings — the spec's (`/api/v1/import/{id}/commit`, no trailing
slash) and the platform's own (`…/commit/`) — pointing at the same view. See
`apps/data_import/urls.py` for why the aliases are there: `APPEND_SLASH` only
rescues a GET, so a POST written against the spec as documented would 404 in
production.

The aliases are correct at runtime and wrong in a schema. An OpenAPI document
keys operations by `operationId`, so every alias produced a second operation
with the same id, which drf-spectacular resolved by appending a numeral —
`api_v1_import_commit_create` and `api_v1_import_commit_create2`. A client
generated from that schema gets two methods for one endpoint, one of them named
after a spelling nobody chose.

The hook drops the schema *entry* for a no-trailing-slash path when an
identical entry exists for the same path plus a slash. Runtime routing is
untouched: this runs on the generated document, in `manage.py spectacular`, and
nothing it does is visible to a request.
"""
import logging

logger = logging.getLogger(__name__)


def drop_trailing_slash_aliases(result, generator, request, public):
    """Remove duplicate operations created by no-slash route aliases.

    A path is dropped only when its trailing-slash twin exists *and* carries the
    same set of HTTP methods. That condition is what keeps this safe: a route
    that deliberately exists only without a slash has no twin, so it is kept.
    """
    paths = result.get('paths') or {}
    dropped = []

    for path in list(paths):
        if path.endswith('/'):
            continue
        twin = f'{path}/'
        if twin not in paths:
            continue
        if set(paths[twin]) != set(paths[path]):
            logger.warning(
                'Schema: %s and %s differ in methods (%s vs %s); keeping both '
                'rather than guessing which is canonical.',
                path, twin, sorted(paths[path]), sorted(paths[twin]),
            )
            continue
        dropped.append(path)
        del paths[path]

    if dropped:
        logger.info(
            'Schema: dropped %d no-slash alias path(s) whose trailing-slash '
            'twin documents the same operations: %s',
            len(dropped), ', '.join(sorted(dropped)),
        )
    return result
