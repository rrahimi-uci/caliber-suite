# REST API authentication and conventions

These pages document the HTTP contract itself: the headers you send, the
envelopes you receive, and the failure shapes you should handle before you build
your own client.

## Authentication modes

CALIBER supports three identity patterns, but only two make sense for direct
programmatic use:

| Mode | How it works | Use it when |
| --- | --- | --- |
| Bearer token | `Authorization: Bearer <token>` | normal automation and external integrations |
| Trusted header | `X-CALIBER-User: <user>` plus optional `X-CALIBER-Proxy-Secret` | only behind a trusted identity proxy in `trusted_header` deployments |
| Browser session | session cookie plus CSRF token | browser traffic and same-origin UI requests, not headless automation |

Personal access tokens are the default automation credential. Trusted headers
carry no proof by themselves and are ignored in the default `session`
deployment mode.

## Core headers

| Header | Meaning |
| --- | --- |
| `Authorization: Bearer <token>` | Personal access token or session token |
| `X-CALIBER-Project: <project_id>` | Active project/workspace scope |
| `X-CALIBER-CSRF: <token>` | Required for state-changing requests when CSRF enforcement applies |
| `X-Request-Id: <id>` | Optional caller-supplied correlation id; CALIBER also returns request ids on responses and errors |
| `Accept: application/json` | Preferred for management API calls |

Example:

```bash
curl -s \
  -H "Authorization: Bearer $CALIBER_TOKEN" \
  -H "X-CALIBER-Project: $CALIBER_PROJECT" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflows"
```

## CSRF

CSRF exists for browser-style credentials. Bearer-token automation generally
does not need to bootstrap it, but cookie-based writes do.

Fetch a token with:

```bash
curl -s \
  -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/csrf"
```

Then echo the returned token in `X-CALIBER-CSRF` on the write request.

## Response envelopes

Most management API responses use the standard envelope:

```json
{
  "data": {
    "workflow_id": "WF-123",
    "name": "release-governance"
  }
}
```

Lists also live under `data`:

```json
{
  "data": [
    {"workflow_id": "WF-123"},
    {"workflow_id": "WF-456"}
  ]
}
```

## Pagination and list conventions

List endpoints commonly accept:

- `?limit=<n>`
- `?offset=<n>`

Invalid values fall back to defaults rather than failing request parsing, so a
consumer that cares about deterministic pagination should always send explicit
values.

## Error shapes

General route failures come back as:

```json
{
  "detail": "forbidden",
  "status_code": 403
}
```

Validation failures use a structured 400 body:

```json
{
  "detail": "request body validation failed",
  "status_code": 400,
  "errors": [
    {
      "loc": ["instructions"],
      "msg": "instructions must reference at least one evaluation variable",
      "type": "value_error"
    }
  ]
}
```

Your client should treat transport failures, HTTP failures, and validation
failures as three different classes of problem.
