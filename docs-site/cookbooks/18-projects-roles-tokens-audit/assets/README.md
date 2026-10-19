# SCN-18 assets

Data files for [the lab](../README.md). The script `cookbook_18_projects_roles_tokens_audit.py`
reads all three directly; nothing here is pasted by hand.

| # | File | What it holds | Used by |
| --- | --- | --- | --- |
| 1 | [`dataset/team-roster.csv`](dataset/team-roster.csv) | nine people: `user_id`, job title, `project_role` (`owner`, `editor`, `reviewer`, `viewer`, or `none` for a non-member), `platform_scopes` (`none`, `operator`, `operator+approver`) and why each is in the roster | the script creates an account for each, adds the members with these roles, and checks every account holds exactly these platform scopes. The UI path creates the same accounts under **Administration** |
| 2 | [`dataset/role-matrix.csv`](dataset/role-matrix.csv) | eight actions and the status each kind of person must get (`owner`, `editor`, `reviewer`, `viewer`, `viewer_no_operator`, `non_member`); `400\|201` means either is a pass | the script sends every person every action (72 requests) and compares the status |
| 3 | [`dataset/failure-scenarios.json`](dataset/failure-scenarios.json) | sixteen refusals, F01 to F16, with the expected status and the text the error must contain; `only_when: csrf_enabled` marks the one that needs a deployment with CSRF on | the script provokes each and compares |

## The roster in one table

| Person | Job | Project role | Platform scopes | What they prove |
| --- | --- | --- | --- | --- |
| maya | support engineering lead | owner (creates the project, so primary) | operator + approver | the primary owner cannot be removed or demoted |
| omar | support operations manager | owner | operator + approver | a second owner manages members |
| priya | prompt engineer | editor | operator | writes and releases; gets the project-bound token |
| sam | prompt engineer | editor | operator | the editor column is about the role |
| quinn | QA lead | reviewer | operator + approver | records evidence and runs, cannot change or release |
| rosa | QA analyst | reviewer | operator | a reviewer without the approver scope |
| victor | product manager | viewer | operator | the 403 names the missing project role |
| wen | compliance auditor | viewer | none | the 403 names the missing platform scope |
| nora | billing platform engineer | none | operator | a non-member: 404 for everything in the project |

The platform scopes are server settings (`CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS`), not
data in a project: see the README, "Prerequisites and setup". The demo password for all nine accounts
is `lab18-demo-passphrase`; it is public, so use it only on a stack nobody else can reach.

## Why a data-driven matrix

The matrix is the same rule written twice: in `caliber/src/caliber/resource_access.py`
(`PROJECT_ACTIONS`) and in this file. A unit test pins that they agree, and the script proves the
running server follows them. If a role gets a different status than the file says, either the product
or the file is wrong; fix whichever it is.
