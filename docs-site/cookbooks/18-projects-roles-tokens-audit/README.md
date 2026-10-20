# Cookbook 18 — Projects, Roles, Tokens & Audit

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery (a preview area, off by default), where this lab is listed and can be installed. The capability itself, projects and roles, access tokens, the audit log, the release timeline and readiness, is part of the supported alpha and needs no flag.

**Track:** Govern · **Level:** Core · **Time:** 45–60 min

**Surfaces:** Administration · Workspaces (the top-bar selector) · Settings › API tokens · Audit Log · Releases · `caliberctl` · Python SDK

## What it demonstrates and the expected outcome

A new team lead has to answer four questions before anyone else touches CALIBER: who can do what in
this project, how automation acts as one person inside one project, what happens when a token is
revoked, expires or is used in the wrong project, and where the record of all of it is.

You build a nine-person support team in a workspace (the UI says *workspace*, the API says
*project*: they are the same thing), give people the `owner`, `editor`, `reviewer` and `viewer`
roles, and prove the role matrix with real requests from a data file. You issue an access token,
bind it to the workspace, use it from `caliberctl` and the Python SDK, and watch it get refused
outside its workspace and after it is revoked or expired. You release one prompt, and read every one
of those actions back from the audit log, the release timeline and the live board, and read
`/health` and `/readiness` to see what is simulated.

You are done when:

- all 72 requests of the role matrix (9 people x 8 actions) get the status code `role-matrix.csv` expects
- the bound token acts in its own workspace, gets **403** in another, and gets **401** once revoked or expired
- the primary owner cannot be removed (**409**) and no role can be escalated by the person asking
- the audit log has one row for each of those actions, filterable by actor, action and entity, and none carries a token secret
- the release timeline and the live board show the prompt release, once, by the editor who made it
- `/readiness` is `ready` and names `eval` and `llm` as simulated
- `cookbook_18_projects_roles_tokens_audit.py` exits 0 and the Playwright spec passes

## Prerequisites and setup

You need a running CALIBER stack (see [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md))
with these settings, on the **server**. They are not optional: they decide what each account may do
at all, before any project role is considered.

| Variable | Value for this lab | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | the gallery lists this lab (the capability needs no flag) |
| `CALIBER_ADMIN_USERS` | `admin` | the only admin: creates accounts and reads the audit log |
| `CALIBER_OPERATOR_USERS` | `admin,maya,omar,priya,sam,quinn,rosa,victor,nora` | the `caliber.operator` scope: may write anything at all |
| `CALIBER_APPROVER_USERS` | `admin,maya,omar,quinn` | the `caliber.approver` scope: with operator, may be an `owner` |

An account holds a password only. The lists above, not the project role, say who is an operator or
an approver; the project role then narrows what that person may do *in one project*. `wen` is on
neither list on purpose (a viewer with no operator scope) and `nora` is an operator who belongs to
no project.

The repository's disposable stack takes the same variables, so from the repository root:

```bash
export CALIBER_ADMIN_USERS=admin
export CALIBER_OPERATOR_USERS=admin,maya,omar,priya,sam,quinn,rosa,victor,nora
export CALIBER_APPROVER_USERS=admin,maya,omar,quinn
scripts/cookbook-env.sh up lab18 --flags cookbooks
source .tmp/cookbook-env/lab18/env.sh
```

The env file sets `CALIBER_URL` (the bare deployment URL), `CALIBER_BASE_URL` (the same, which is
what the SDK and `caliberctl` read) and `CALIBER_API` (the URL with the `/ajax-api/2.0/mlflow/caliber`
prefix, which the `curl` commands below use). On your own server set the same two:

```bash
export CALIBER_BASE_URL=http://127.0.0.1:5001       # the bare deployment URL, not the /ajax-api path
export CALIBER_API="$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber"
```

Finally an **admin** personal access token for the script (the first token has to come from a
password login; Settings › API tokens does the same in the browser):

```bash
SESSION=$(curl -s -c - -o /dev/null -X POST "$CALIBER_API/auth/login" \
  -H 'Content-Type: application/json' -d '{"user_id":"admin","password":"admin"}' \
  | awk '/caliber_session/ {print $7}')
export CALIBER_TOKEN=$(curl -s -X POST "$CALIBER_API/auth/tokens" \
  -H "Authorization: Bearer $SESSION" -H 'Content-Type: application/json' \
  -d '{"name":"lab18-admin"}' | jq -r .data.token)
```

You also need `jq` for the by-hand tour, `caliber/.venv` (`make install`) for the SDK, `caliberctl` and the script,
and, for the UI path, `npm ci` in `caliber/caliber-ui`.

**Simulated versus real providers.** By default `CALIBER_LLM_PROVIDER=fake` and
`CALIBER_EVAL_PROVIDER=fake`, so nothing in this lab calls a model. Almost everything here is about
identity, roles, tokens and records, which a real provider would not change. The one place it shows
is the `evaluation.run` row of the matrix: a project member allowed to run an evaluation gets
**400** `evaluation requires a real LLM provider` (the request passed the role gate and then met the
simulated provider), where a viewer gets **403**. With a real provider the 400 becomes a **201**; the
matrix file accepts either (`400|201`), and the lab asserts the role gate, never a score.

## Dataset and sample data

All three files are in [`assets/dataset/`](assets/dataset/) and the script reads them directly.

| File | What it contains | Why it exists |
| --- | --- | --- |
| [`team-roster.csv`](assets/dataset/team-roster.csv) | nine people: two owners, two editors, two reviewers, two viewers, one non-member, each with a job title, a project role and the platform scopes they hold | a realistic team in which every role has two people, so a column of the matrix is about the role and not one person. `victor` (a viewer who is an operator) and `wen` (a viewer who is not) show the two different 403s |
| [`role-matrix.csv`](assets/dataset/role-matrix.csv) | eight actions (read, list members, create a test set, create a prompt, release a prompt, run an evaluation, edit the project, change a member's role) and the status code each kind of person must get | the expectations the script asserts for all 72 person-by-action requests; the columns are `owner`, `editor`, `reviewer`, `viewer`, `viewer_no_operator` and `non_member` |
| [`failure-scenarios.json`](assets/dataset/failure-scenarios.json) | sixteen refusals (F01–F16): the status and the text each error must contain | the scenarios the script provokes: owner removal, role escalation, token scope escalation, bound-token misuse, revoked, expired and made-up tokens, a wrong password, the audit-log permission, and CSRF |

The matrix is a restatement of [`docs/build/auth-and-project-scoping.md`](../../../docs/build/auth-and-project-scoping.md)
and `caliber/src/caliber/resource_access.py` (`PROJECT_ACTIONS`); a unit test pins that the file and
the rules agree. The demo password is `lab18-demo-passphrase` for every roster account: it is public
in this repository, so create these accounts only on a stack nobody else can reach.

## Configuration and environment

| Variable | Used by | Value in this lab |
| --- | --- | --- |
| `CALIBER_BASE_URL` | script, SDK, `caliberctl` | the bare deployment URL, for example `http://127.0.0.1:5001` |
| `CALIBER_TOKEN` | script (admin), then the bound token for `caliberctl` | an admin personal access token (script); the script substitutes the bound token itself for the CLI |
| `CALIBER_LAB18_PASSWORD` | script | `lab18-demo-passphrase` (the default; set it only if you created the accounts with another password) |
| `CALIBER_PROJECT` | SDK, `caliberctl` | **leave unset**: the lab shows a bound token acting in its own project, and an ambient project would hide that |
| `CALIBER_CSRF_ENABLED`, `CALIBER_CSRF_SIGNING_SECRET_ENV` | server, optional | off by default; set on the server to see scenario F16 (see [CSRF](#csrf-when-the-deployment-enables-it)) |
| `CALIBER_LAB_BASE_URL` | Playwright | `$CALIBER_URL/caliber` |

## Run it through the API

### The whole lab in one command

From the repository root, with the setup above:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  -m examples.cookbooks.cookbook_18_projects_roles_tokens_audit
```

It takes about ten seconds (three of them waiting for a short-lived token to expire), prints one line
per check, ends with a JSON summary, and exits 0 only when every check passed. Captured on the
disposable stack (ids and the run key vary; lines trimmed to fit):

```text
ok   the token is an admin  [admin]
ok   GET /health reports ok  [{'status': 'ok', 'version': '0.1.0a1', 'db': 'ok'}]
ok   GET /readiness is ready  [blockers: []]
     simulated providers: ['eval', 'llm']
ok   readiness names every simulated provider  [simulated: eval, llm]
ok   every roster account exists  [9 created, 0 already there]
ok   every account holds exactly the platform scopes the roster lists
ok   the creator is the project's primary owner
ok   the member list is exactly the roster  [8 members]
ok   every person sees the role the roster gave them (the outsider sees 404)
ok   the role matrix holds (72 requests: 9 people x 8 actions)
ok   the token carries its project binding  [PAT-ffcb6a76]
ok   the secret is only on the issuing response, never in the list
ok   the bound token acts as its owner  [priya]
ok   with no project named, it acts in its bound project
ok   the prompt is released to @prod by the token  [REL-f83fab39]
ok   cookbook 18 is in the catalog with its guided steps  [10 steps]
ok   the lab installs into the project through the bound token  [WF-bff0b770]
ok   a viewer's prompt inventory lists the project's prompts  [7 prompts]
ok   a non-member's prompt inventory shows nothing from the project
ok   caliberctl whoami sees the same identity  [['caliber.operator']]
ok   caliberctl token list shows the bound token
ok   caliberctl prompt list shows the released prompt
ok   the release timeline has exactly one promote event for the prompt, by priya  [...]
ok   the live board lists the prompt at v1 on @prod  [...]
ok   F01 The primary owner cannot be removed  [409 the primary owner cannot be removed]
ok   F02 The primary owner cannot be demoted  [409 the primary owner cannot be changed here; use transfer-ownership]
ok   F03 An editor cannot promote themselves to owner  [403 project role 'editor' cannot perform project.manage_members]
ok   F04 A reviewer cannot add a member  [403 project role 'reviewer' cannot perform project.manage_members]
ok   F05 An owner cannot grant owner to someone without both platform scopes  [409 'victor' lacks the caliber.operator + caliber.approver scopes required for the owner role]
ok   F06 An unknown project role is rejected  [400 role must be owner, editor, reviewer, or viewer]
ok   F07 A token cannot be issued with a scope its owner lacks  [403 cannot grant scopes you do not hold: ['caliber.admin']]
ok   F08 A non-member cannot bind a token to the project  [404 project 'PRJ-712fce8d' not found]
ok   F09 A viewer-scoped token cannot write even for an owner  [403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']]
ok   F10 A bound token is refused for another project  [403 this personal access token is bound to project 'PRJ-712fce8d' and cannot act on project 'P...]
ok   the bound token still works in its own project after the refusal
ok   revoking reports a live token was revoked
ok   F11 A revoked token stops authenticating  [401 authentication required (no identity in request)]
ok   whoami does not raise for a revoked token: it answers anonymous
ok   a token works before it expires  [200]
ok   F12 An expired token stops authenticating  [401 authentication required (no identity in request)]
ok   F13 A made-up token is rejected  [401 authentication required (no identity in request)]
ok   F14 A wrong password does not sign in  [401 invalid credentials]
ok   F15 Only an admin can read the audit log  [403 missing required scope; expected one of ['caliber.admin'], have ['caliber.approver', 'cali...]
skip F16: CSRF enforcement is off on this deployment (the default)
ok   a removed member gets 404 straight away  [404]
ok   the member list no longer shows them
ok   audit: create_project by maya for this project
ok   audit: add_project_member once per member (7 added by maya)  [7]
ok   audit: the roles recorded match the roster
ok   audit: update_project_member rows name the owners who edited
ok   audit: update_project rows name the owners and editors who edited
ok   audit: remove_project_member victor by maya
ok   audit: refused requests leave no project audit rows  [[]]
ok   audit: the bound token's creation names its project, and its revocation is recorded
ok   audit: no token secret is in any audit row
ok   audit: promote_prompt for the released prompt, by priya, not overridden
ok   audit: create_account by admin for every roster account
ok   audit: sign_in rows name who signed in
ok   the sessions the script opened are revoked  [9]
{
  "bound_token_id": "PAT-ffcb6a76",
  "csrf_enabled": false,
  "failure_scenarios": { "F01": "409", "F02": "409", "F03": "403", ... "F15": "403" },
  "matrix_outcomes": { "editor:200": 8, "editor:201": 4, "editor:400": 2, "editor:403": 2, "non_member:404": 8, ... },
  "matrix_requests": 72,
  "members": 8,
  "project_id": "PRJ-712fce8d",
  "released_prompt": "lab18-f01ea1-support-reply",
  "simulated_providers": ["eval", "llm"]
}
```

A second run is safe: the accounts already exist (the first line then reads `0 created, 9 already
there`), each run makes its own uniquely named projects, and the sessions it opened are revoked at the end.

The role matrix is the heart of the lab. For every person in the roster and every row of
`role-matrix.csv` the script sends the request as that person and compares the status. In summary
(`matrix_outcomes` in the JSON counts them):

| Action (project action) | owner | editor | reviewer | viewer | viewer, no operator | non-member |
| --- | --- | --- | --- | --- | --- | --- |
| read the project, list members (`read`) | 200 | 200 | 200 | 200 | 200 | 404 |
| create a test set (`resource.write.evidence`) | 201 | 201 | 201 | 403 | 403 | 404 |
| create a prompt (`resource.write.runtime`) | 201 | 201 | 403 | 403 | 403 | 404 |
| release a prompt to `@prod` (`release.apply`) | 200 | 200 | 403 | 403 | 403 | 404 |
| run an evaluation (`resource.execute`) | 400\* | 400\* | 400\* | 403 | 403 | 404 |
| edit the project (`project.update`) | 200 | 200 | 403 | 403 | 403 | 404 |
| change a member's role (`project.manage_members`) | 200 | 403 | 403 | 403 | 403 | 404 |

\* the simulated provider's answer, which a real provider turns into 201; it is not a 403.

Two details worth seeing for yourself. A **non-member gets 404, not 403**: the project does not
exist as far as they can tell. And a **viewer's 403 differs by platform scope**: `victor` (an
operator) is refused by the *project role* (`project role 'viewer' cannot perform
release.apply`), while `wen` (no operator scope) is refused by the *platform scope* first
(`missing required scope; expected one of ['caliber.operator']`). Both layers have to allow a write.

### The same ideas by hand

Each idea above with plain `curl`, `caliberctl` and the SDK, so you can see the requests. The
helpers sign a person in and call the API as them; the session token a login returns is accepted as a
bearer token. These snippets are `bash` (zsh does not split the unquoted `${5:+...}` the same way), so run
`bash` first if your shell is zsh.

```bash
export LAB18_PASSWORD=lab18-demo-passphrase
login() {   # login USER -> that user's session token
  curl -s -c - -o /dev/null -X POST "$CALIBER_API/auth/login" -H 'Content-Type: application/json' \
    -d "{\"user_id\":\"$1\",\"password\":\"$LAB18_PASSWORD\"}" | awk '/caliber_session/ {print $7}'
}
call() {    # call TOKEN METHOD PATH [JSON] [PROJECT] -> body, then [status]
  curl -s -w '\n[%{http_code}]\n' -X "$2" "$CALIBER_API$3" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    ${5:+-H "X-CALIBER-Project: $5"} ${4:+-d "$4"}
}
MAYA=$(login maya); PRIYA=$(login priya); VICTOR=$(login victor); NORA=$(login nora)
```

(The accounts exist once the script or the UI path has run; otherwise create them under
Administration or with `POST /auth/accounts`, as the admin.)

Create the project as `maya`, which makes her its **primary owner**, and add an editor and a viewer:

```bash
PROJECT=$(call "$MAYA" POST /projects '{"name":"lab18-tour"}' | head -1 | jq -r .data.project_id)
call "$MAYA" POST "/projects/$PROJECT/members" '{"user_id":"priya","role":"editor"}' | head -1 | jq -c '.data | {user_id, role, status}'
call "$MAYA" POST "/projects/$PROJECT/members" '{"user_id":"victor","role":"viewer"}' | head -1 | jq -c '.data | {user_id, role, status}'
```

```text
{"user_id":"priya","role":"editor","status":"active"}
{"user_id":"victor","role":"viewer","status":"active"}
```

What each person sees, and who may write (a prompt):

```bash
call "$PRIYA"  GET "/projects/$PROJECT"                 # access_role: editor   [200]
call "$NORA"   GET "/projects/$PROJECT"                 # project ... not found [404]
call "$VICTOR" POST /prompts '{"name":"tour-a","template":"Reply to {{customer}}."}' "$PROJECT"
call "$NORA"   POST /prompts '{"name":"tour-a","template":"Reply to {{customer}}."}' "$PROJECT"
call "$PRIYA"  POST /prompts '{"name":"tour-b","template":"Reply to {{customer}}."}' "$PROJECT"
```

```text
{"detail":"project role 'viewer' cannot perform resource.write.runtime","status_code":403}
[403]
{"detail":"project 'PRJ-bf872ba8' not found","status_code":404}
[404]
{"data":{"name":"tour-b","version":1,"uri":"prompts:/tour-b/1","template_preview":"Reply to {{customer ...
[201]
```

Issue a token for `priya` and **bind it to the project**, then use it from `caliberctl` and the SDK.
The secret is shown once. With no `--project` and no `CALIBER_PROJECT` the token acts in its bound project:

```bash
ISSUED=$(call "$PRIYA" POST /auth/tokens \
  '{"name":"support-ci","scopes":["caliber.operator"],"project_id":"'$PROJECT'"}' | head -1)
echo "$ISSUED" | jq -c '.data | {token_id, user_id, scopes, project_id, active}'
export CALIBER_TOKEN=$(echo "$ISSUED" | jq -r .data.token); TOKEN_ID=$(echo "$ISSUED" | jq -r .data.token_id)
caliber/.venv/bin/caliberctl whoami
caliber/.venv/bin/caliberctl --project "$PROJECT" prompt list
```

```text
{"token_id":"PAT-38e34401","user_id":"priya","scopes":["caliber.operator"],"project_id":"PRJ-bf872ba8","active":true}
user_id   priya
scopes    caliber.operator
is_admin  no
PROMPT_NAME  AGENT_ID    VERSION  ALIAS  SOURCE
tour-27378   tour-27378  -        prod   mlflow
```

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python - <<PY
import os
from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberPermissionError
with CaliberClient(os.environ["CALIBER_BASE_URL"], token=os.environ["CALIBER_TOKEN"]) as c:
    print("whoami:", c.me.get().user_id, "| project role:", c.projects.get("$PROJECT").access_role)
    try:
        c.projects.get("PRJ-some-other-project")
    except CaliberPermissionError as e:
        print("other project ->", e.status_code, e.detail)
PY
```

```text
whoami: priya | project role: editor
other project -> 403 this personal access token is bound to project 'PRJ-bf872ba8' and cannot act on project 'PRJ-some-other-project'
```

Revoke it; it stops authenticating at once. (`caliberctl whoami` and `GET /me` answer `anonymous`
rather than an error for a bad token, so check a route that needs a user, as here.)

```bash
call "$PRIYA" DELETE "/auth/tokens/$TOKEN_ID"
curl -s -w '\n[%{http_code}]\n' "$CALIBER_API/projects" -H "Authorization: Bearer $CALIBER_TOKEN"
```

```text
{"data":{"token_id":"PAT-38e34401","revoked":true}}
[200]
{"detail":"authentication required (no identity in request)","status_code":401}
[401]
```

Read the **audit log**. It is admin-only (`maya`, an owner, is refused). Filter by `entity_id`,
`actor`, `action`, `entity_type`, `since` and `until`:

```bash
ADMIN=$(curl -s -c - -o /dev/null -X POST "$CALIBER_API/auth/login" -H 'Content-Type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' | awk '/caliber_session/ {print $7}')
call "$ADMIN" GET "/audit-log?entity_id=$PROJECT&limit=50" | head -1 \
  | jq -r '.data.entries | reverse | .[] | "\(.actor)\t\(.action)\t\(.details | tostring)"'
call "$ADMIN" GET "/audit-log?action=create_personal_access_token&actor=priya&limit=1" | head -1 \
  | jq -c '.data.entries[0] | {actor, action, entity_type, entity_id, details}'
call "$MAYA" GET '/audit-log?limit=1'
```

```text
maya	create_project	{"name":"lab18-tour-5693","storage_backend":"local","slug":"lab18-tour-5693"}
maya	add_project_member	{"user_id":"priya","role":"editor"}
maya	add_project_member	{"user_id":"victor","role":"viewer"}
{"actor":"priya","action":"create_personal_access_token","entity_type":"personal_access_token","entity_id":"PAT-38e34401","details":{"scopes":["caliber.operator"],"expires_at":null,"project_id":"PRJ-bf872ba8"}}
{"detail":"missing required scope; expected one of ['caliber.admin'], have ['caliber.approver', 'caliber.operator', 'caliber.viewer']","status_code":403}
[403]
```

Note what is **not** in the log: the token's secret, and the refused requests (a 403 changes nothing, so
it writes nothing; a refused write shows up in the server's request log instead).

Finally health and readiness, with no credential at all:

```bash
curl -s "$CALIBER_API/health"
curl -s "$CALIBER_API/readiness" | jq -c '.data | {ready, blockers, simulated, all_real}'
curl -s "$CALIBER_API/readiness" | jq -c '.data.checks[] | select(.name=="providers" or .name=="schema") | {name, ready, required, detail}'
```

```text
{"status":"ok","version":"0.1.0a1","db":"ok"}
{"ready":true,"blockers":[],"simulated":["eval","llm"],"all_real":false}
{"name":"providers","ready":false,"required":false,"detail":"simulated: eval, llm"}
{"name":"schema","ready":true,"required":true,"detail":"schema at head 0113"}
```

`/health` says the process and database are up; `/readiness` adds MLflow, the schema version and the
worker queue, and answers **503** when a required dependency is missing. The `providers` check is
`ready: false` but not `required`: that is how a simulated provider is reported, and it never blocks.

### CSRF, when the deployment enables it

CSRF enforcement is off by default (`CALIBER_CSRF_ENABLED`). When a deployment turns it on, every
`POST`, `PATCH`, `PUT` and `DELETE` needs an `X-CALIBER-CSRF` header, **bearer-token writes included**.
The script detects it (`GET /csrf` answers `enabled: true`) and then also runs F16. Captured on a stack
started with `CALIBER_CSRF_ENABLED=true CALIBER_CSRF_SIGNING_SECRET_ENV=LAB18_CSRF_SECRET LAB18_CSRF_SECRET=<long random string>`:

```bash
curl -s -X POST "$CALIBER_API/projects" -H "Authorization: Bearer $CALIBER_TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"csrf-demo"}'
CSRF=$(curl -s -H "Authorization: Bearer $CALIBER_TOKEN" "$CALIBER_API/csrf" | jq -r .data.token)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$CALIBER_API/projects" -H "Authorization: Bearer $CALIBER_TOKEN" \
  -H "X-CALIBER-CSRF: $CSRF" -H 'Content-Type: application/json' -d '{"name":"csrf-demo"}'
```

```text
{"detail":"CSRF check failed: missing CSRF token","status_code":403}
201
```

The SDK fetches a token and replays a refused write once, so none of the SDK calls above change; login
itself is also a protected write, so the script sends the anonymous token `GET /csrf` issues for it.

## Run it through the UI

Sign in at `$CALIBER_URL/caliber/login`. Each person signs in separately (use a private window per
person, or **Log out** between them); the password for every roster account is `lab18-demo-passphrase`.
The UI calls a project a **workspace**. The sidebar groups are **Prompt lifecycle**, **Agent
resources**, **Operate**, **Integrations** and **Admin**.

1. **Create the team's accounts.** As `admin` / `admin`: **Admin › Administration › Accounts**. For each of the nine people in `team-roster.csv`, type the **User ID** and the **Password** and click **Create account**. A green note says `Created maya.` and the account appears in the table as **Active**. (An account left **Disabled** by an earlier cleanup shows an **Enable** button.)
2. **Create the workspace.** Sign in as `maya`. In the top bar click the **+** beside the workspace selector (**Create workspace**), type a **Workspace name** such as `lab18-support-copilot`, and click **Create and select**. The selector now shows it, and `maya` is its primary owner.
3. **Add the members with their roles.** As `maya`: **Admin › Administration › Project access**. The **Project for access management** selector shows the new workspace. For each of the seven other members type the **User ID**, choose the **Role** (**Owner (Admin)**, **Editor**, **Reviewer** or **Viewer**) and click **Add member**. The table lists `maya (primary owner)` with no **Remove** button, and each member with the role you gave. `nora` is not added. (The page also shows `missing required scope ... caliber.admin` errors in **Accounts** and **Secrets**: `maya` is an owner, not an admin, and the project section works regardless.)
4. **Prove a viewer and a reviewer are refused, and an editor can write, release and revise.** Sign in as `victor` (viewer), pick the workspace in the top bar, open **Prompt lifecycle › Prompts**, click **New prompt › Write / paste**, fill a **Prompt name** and **Prompt text**, and click **Create and Open**: the page shows `project role 'viewer' cannot perform resource.write.runtime`. Under **Administration › Project access** he sees the member list but only the line **Only a project Admin can manage members.** and no **Add member** form. Then sign in as `priya` (editor), pick the workspace and create the prompt the same way: it opens with **v1** marked **Live**, because creating a prompt releases its first version to `@prod`. Still as `priya`, go back to **Prompts**, search for the prompt, click **Edit**, change the **Template** (for example add `and name the next step`), click **Save new version** (a new immutable version; nothing live changes yet) and then **Promote v2 to @prod**. Open the prompt: **v2** is **Live** and **v1** is not. Finally sign in as `quinn` (reviewer) and try the same prompt creation: `project role 'reviewer' cannot perform resource.write.runtime`. A reviewer can read and review, not author.
5. **Prove a non-member sees nothing.** Sign in as `nora`. Her workspace selector holds only **All workspaces**: the project is not listed at all.
6. **Change a role and remove a member.** As `maya`: **Admin › Administration › Project access**, choose the workspace. Set the **Role for sam** select to **Viewer**: a note says `Changed sam to viewer.` Click **Remove** on `victor`'s row: `Removed victor from the project.` and the row is gone. Sign in as `sam` and try to create a prompt: refused, `project role 'viewer' cannot perform resource.write.runtime` (the demotion took effect at once). `victor`'s workspace selector no longer lists the workspace.
7. **Issue a token bound to the workspace.** As `priya`: **Admin › Settings › API tokens**. Give the token a **Token name** (`support-ci`), leave **Expires** at **30 days**, choose the workspace in **Workspace** (it lists each workspace with your role, for example `lab18-support-copilot (editor)`), tick **caliber.operator**, and click **Create token**. A green box says **Copy this token now. It will not be shown again.** and shows the secret (`calpat_...`). Click **I have copied it**: the box and the secret are gone for good. **Your tokens** now lists `support-ci` as **Active** with its scopes and workspace.
8. **Revoke the token.** On the `support-ci` row click **Revoke**, read the confirmation (**Anything using this token stops working immediately. This cannot be undone.**), and click **Revoke token**. The row now reads **Revoked** and its **Rotate** and **Revoke** buttons are gone.
9. **Read the audit log.** As `admin`: **Operate › Audit Log**. First find the workspace's id: set **Actor** `maya` and **Action** `create_project`, and the one row whose details name your workspace shows its id in the **Entity** column (`project · PRJ-...`). Clear both, put that id in **Entity ID**: one `create_project` row by `maya`, seven `add_project_member` rows whose details name each role, one `update_project_member` (sam) and one `remove_project_member` (victor). Add **Action** `add_project_member` to see only the grants. Clear **Entity ID**, set **Actor** `priya` and **Action** `promote_prompt`: two rows for the prompt, its two releases. Change **Action** to `create_personal_access_token`: one row whose details carry the workspace's id and no secret. Change **Action** to `revoke_personal_access_token` for the revocation. As `maya`, the same page says **Admin access required**.
10. **Read the release timeline and the live board.** As anyone with access: **Prompt lifecycle › Releases**. **What's live now** has a row for the prompt at `v2` on `@prod`, by `priya`; **Timeline** has two **Promote** events for it (v2, then v1), both by `priya`. The timeline and live board list prompt releases across workspaces (the registry is not workspace-scoped), so you will also see other labs' prompts.
11. **Read readiness: the health dot and the simulated-provider banner.** On any page the amber banner says **Simulated providers: eval, llm are running in fake mode** and **Evaluation scores are canned, not measurements.**, and the green dot in the top bar (**API and database reachable**) is `/health`. A missing dependency would add a red **Not ready** line from `/readiness`.

The same steps are performed, with real clicks and the assertions above, by the Playwright spec:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/18-projects-roles-tokens-audit.spec.ts
```

It also checks that the **Cookbooks** gallery (`CALIBER_FEATURE_COOKBOOKS`) lists this lab as **Projects, Roles, Tokens & Audit** with the same eleven guided steps. Installing it from the gallery creates a paused workflow in the active workspace; the lab does not need it.

## Verify it worked

API (all done by the script; each can be read by hand):

- **Roles.** `GET /projects/{id}` answers each person's `access_role`: owner, owner, editor, editor, reviewer, reviewer, viewer, viewer, and **404** for `nora`. `GET /projects/{id}/members` is the roster (eight members, `maya` is `project.owner`).
- **Matrix.** the script's `the role matrix holds (72 requests: 9 people x 8 actions)` line, and `matrix_outcomes` in the summary: `owner:403` and `non_member:200` do not appear.
- **Token.** `GET /auth/tokens` (as `priya`) lists the token with `project_id` set, `scopes` `["caliber.operator"]`, `active: false` after revocation and **no `token` key**. A request with it and another `X-CALIBER-Project` is 403; after revocation or expiry any request is 401.
- **Audit.** `GET /audit-log?entity_id=<project>` shows `create_project`, seven `add_project_member`, the `update_project` and `update_project_member` rows (by the owners and editors the matrix let through) and `remove_project_member` for `victor`; `GET /audit-log?entity_type=personal_access_token&actor=priya` shows the create and revoke; no row carries the secret.
- **Release.** `GET /releases/timeline?entity_type=prompt` has one `promote_prompt` for the lab's prompt (`actor: priya`, `overridden: false`); `GET /releases/live` lists it at `v1`, alias `prod`.
- **Readiness.** `GET /readiness` is `ready: true`, `blockers: []`, `simulated: ["eval","llm"]`.

UI: the eleven steps above, asserted by `18-projects-roles-tokens-audit.spec.ts` (passes in about fifteen seconds on a warm stack). The counts to expect on the **Audit Log** with **Entity ID** set to the workspace: 1 `create_project`, 7 `add_project_member`, 1 `update_project_member`, 1 `remove_project_member`. (The API script reaches 72 requests and the token behaviour, which have no screen: a token is used by a program, so its use is an API/CLI step. The UI path proves the same rules a person can see.)

Offline (no server): `caliber/.venv/bin/python -m pytest sdk/caliber-sdk/tests/test_cookbook_18_roles_tokens_audit.py sdk/caliber-sdk/tests/test_cookbook_examples.py -k cookbook_18` runs the same script against a stand-in server that enforces the role rules, and checks that the data files and the rules agree and that the script **fails** when a server breaks a rule.

## Common errors and troubleshooting

| What you see | Cause and fix |
| --- | --- |
| script: `NOT VERIFIED ... quinn holds [...] but the roster needs [...]: restart CALIBER with CALIBER_OPERATOR_USERS and CALIBER_APPROVER_USERS` | the platform scopes of the roster are not set on the server. Export the two lists from [Prerequisites and setup](#prerequisites-and-setup) and `scripts/cookbook-env.sh restart lab18` |
| script: `could not sign in as 'maya': 401 invalid credentials` | the account exists from an earlier run with another password. Reset it under **Administration** (**Reset password for maya**), or set `CALIBER_LAB18_PASSWORD` to the password it has |
| script: `CALIBER_TOKEN must be an admin personal access token` | the token was issued by a non-admin. Create it as `admin` (the setup block above) |
| script: `KeyError: 'CALIBER_BASE_URL'` or every call is 404 | `CALIBER_BASE_URL` unset, or set to the `.../ajax-api/2.0/mlflow/caliber` URL. It must be the bare deployment URL |
| `404` `feature_disabled` on `/cookbooks` | `CALIBER_FEATURE_COOKBOOKS` is off. The role, token and audit parts of the lab still run; only the gallery and the install step need the flag |
| `403` `project role 'viewer' cannot perform ...` | the person's project role is too weak. This is the lab working; ask an owner for a stronger role under **Administration › Project access** |
| `403` `missing required scope; expected one of ['caliber.operator']` | the account has no platform operator scope, whatever its project role. It is on the server's `CALIBER_OPERATOR_USERS` list or it is not |
| `404` `project ... not found` for someone you added | they were removed, or the project is archived, or the request names the wrong id. A non-member always gets 404, never 403 |
| `409` `... lacks the caliber.operator + caliber.approver scopes required for the owner role` | only someone on both lists may be an `owner`. Add them to `CALIBER_APPROVER_USERS` or give them `editor` |
| `409` `the primary owner cannot be removed` / `... changed here; use transfer-ownership` | the project's creator is protected. Make another owner, then `POST /projects/{id}/transfer-ownership` |
| `403` `this personal access token is bound to project ...` | you named a different project from the one the token is bound to. Use the bound project, or issue an unbound token |
| `401` `authentication required` with a token that worked | it was revoked, it expired, or it is mistyped. Check **Settings › API tokens** (**Revoked**, **Expired**). `GET /me` will not tell you: it answers `anonymous` |
| `403` `CSRF check failed: missing CSRF token` | the server enforces CSRF. Send `X-CALIBER-CSRF` (from `GET /csrf`) on every write; the SDK does it for you |
| `403` `Admin access required` on **Audit Log** | only `caliber.admin` reads the audit log, project owners included |
| `503` from `/readiness` | a required dependency is down; the `blockers` array names it (database, MLflow, the schema, the worker queue) |
| the banner says `Simulated providers: eval, llm` | expected here: scores are canned, and nothing in this lab depends on them. Set `CALIBER_LLM_PROVIDER` and `CALIBER_EVAL_PROVIDER` for real behaviour |
| the matrix row `evaluation.run` is `400` for a reviewer | the simulated provider (`evaluation requires a real LLM provider`), after the role gate allowed the request. A viewer's is `403` |

### Limitations the lab shows rather than hides

- **Traces and prompt releases are not project-scoped.** The release timeline lists prompt promotions from every project, and traces are open to every signed-in user (see [`docs/build/auth-and-project-scoping.md`](../../../docs/build/auth-and-project-scoping.md), "What the alpha does not enforce"). The prompt *inventory* is scoped: a non-member sees none of a project's prompts.
- **Refused requests are not audited.** The audit log records what changed.
- **Apply has no second approver.** An owner or editor releases in one step; the audit row names who.

## Cleanup and reset

Everything the lab created is two projects, some prompts, test sets and a workflow draft, a few tokens, some sessions and nine accounts. From the repository root, with the admin `CALIBER_TOKEN` and the same variables:

```bash
# archive the projects named lab18-* (their owner, maya, is the only one who can) and disable the nine accounts
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
  -m examples.cookbooks.cookbook_18_projects_roles_tokens_audit --cleanup
```

```text
archived lab18-billing-assistant-538ac3 (PRJ-0db0cee8)
archived lab18-support-copilot-538ac3 (PRJ-cb115724)
disabled 9 roster account(s)
{"archived_projects": 2, "disabled_accounts": 9}
```

Disabling an account also revokes its sessions; the next run of the script re-enables the accounts it
needs. The script revokes its own tokens. In the UI, revoke any token you left under **Settings ›
API tokens**, and **Disable** the nine accounts under **Administration**. Nothing is deleted: an
archived project is hidden, and the audit log keeps its history by design.

To reset completely on the disposable stack:

```bash
scripts/cookbook-env.sh down lab18
rm -rf .tmp/cookbook-env/lab18
```
