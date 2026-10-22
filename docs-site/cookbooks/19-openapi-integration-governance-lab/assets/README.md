# SCN-19 assets

Everything the lab runs, from the [README](../README.md). There is nothing to author beyond the
upstream itself: you import the lab's own spec and curate the operations it discovers.

| Folder | Files | Used for |
| --- | --- | --- |
| [`server/`](server/) | `ticket_api.py` | the upstream API CALIBER calls over real loopback HTTP: serves its own OpenAPI document and the three ticket operations it describes, behind a bearer token |
| [`dataset/`](dataset/) | `ticket-api-v1.yaml`, `ticket-api-v2.yaml` | the two spec versions the upstream serves at `/openapi.yaml`, before and after `POST /admin/bump-spec`; identical copies of the strings embedded in `ticket_api.py` |
| [`workflow/`](workflow/) | `file-ticket.workflow.yaml` | a one-tool workflow binding the published write tool; fill `__TOOL_NAME__` (the script does) |

## The upstream's three operations

| Operation | Side effect (derived automatically) | What it does |
| --- | --- | --- |
| `GET /tickets` (`listTickets`) | read | list tickets; v2 adds an optional `status` filter |
| `GET /tickets/{ticket_id}` (`getTicket`) | read | one ticket by id, or 404; v2 marks it `deprecated` |
| `POST /tickets` (`createTicket`) | write | create a ticket; v1 requires only `title`, v2 also requires `priority` at runtime |

`createTicket`'s 201 response carries an OpenAPI `links` entry naming `getTicket` and mapping its
`ticket_id` field to that operation's path parameter -- the one **explicit** dependency source
CALIBER's detector auto-wires at high confidence and never offers for review. The same three
operations also share one tag (`tickets`), which the detector's weakest signal (same-tag grouping)
turns into one or more low-confidence, advisory dependencies that an operator can confirm or reject.

## v1 -> v2, and what the diff does and does not catch

CALIBER's reimport diff compares a fixed set of normalized fields (summary, side-effect level,
`deprecated`, auth, whether a body is required, content types, response statuses, tags) -- not every
field. v2 makes three changes, deliberately of three kinds:

| Change | Tracked field? | Diff reports it as |
| --- | --- | --- |
| `getTicket` marked `deprecated: true` | yes (`deprecated`) | **breaking** ("operation was deprecated") |
| `listTickets`'s summary reworded | yes (`summary`) | changed, not breaking |
| `createTicket`'s body gains a *required* `priority` field | no -- only *whether* a body is required is tracked, not which fields within it are | **nothing**. The diff reports zero changes for `createTicket`, even though the published write tool now fails at runtime without `priority` |

That third row is the lab's point: the diff is a real, useful signal, and it is not a substitute for
re-running a published tool's calibration after a reimport.

## The upstream's own request log

`ticket_api.py` keeps no database -- tickets live in memory and are gone on restart -- but it does
append one JSON line (`{"method", "path", "ok"}`) to `TICKET_API_LEDGER` for every call it actually
executes. A call CALIBER refuses (an unready draft, a blocked preview, an egress refusal) never
reaches the process at all, so it never appears here. That is the same "the ledger is the proof, not
CALIBER's own answer" pattern cookbook 05 uses for its MCP server's call ledger.
