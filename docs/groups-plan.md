# Groups — plan

Every dataset and dashboard lives in a **group**. Names are unique only within
a group. A reference is `group:dataset`, and a bare `dataset` means the
dashboard's own group. The UI works in one group at a time. Every user gets a
personal group, `_<username>`.

Shipped as **one change** (storage, personal groups, references and the read
check together) in 0.24.0. All open questions are answered (two review rounds).
The authoritative spec is now `architecture.md` → Portal mode → Groups.

---

## 1. Groups are a security boundary

- **Admins** see and edit everything: every group, personal ones included.
- **A user** works in their **own group** (`_<username>`): creates, edits and
  deletes dashboards and datasets there.
- **Shared (non-user) groups** are managed by admins for now. Membership and
  write access for other users come later.
- **Sharing goes through roles**, for now. No dataset is readable by default:
  its owner grants it to a role in the dataset's access mode (the existing
  `permissions:`), which offers every existing role. Only admins create roles
  and assign them to users. Row filters in a grant still apply, per
  `(group, dataset)`.
- **Roles, users and access stay global**: one Access tab, one Users tab.

## 2. What a user sees

- **Every group** is listed in the group dropdown. A group with nothing shared
  with the user is shown **inactive** (greyed), but can still be opened.
- **Every dashboard** of the open group is listed:
  - **Active** if every managed dataset it reads is shared with the viewer. A
    dashboard that reads only inline data, or no data at all, is active for
    everyone, so a new blank dashboard is visible to all at once.
  - **Inactive** (greyed, at the end of the list) if any dataset it reads isn't
    shared with the viewer.
- **Inactive is only how the list looks.** Anyone can open an inactive group
  or dashboard. What they can't read shows as locks.
- **A reference the viewer can't read** renders the existing lock
  (`_no_access_html`) instead of data.
- **A dashboard the viewer can't write** opens **view-only**: view mode only,
  rendered from the stored YAML, and the YAML is never sent to the browser.
- **Someone else's dataset** shows its column names and types, and example
  values computed **after the viewer's row filters**. Service columns,
  versions and the access mode are hidden.
- **Filter value lists** apply the viewer's row filters, so they never show a
  value the viewer can't see.
- **The assistant** gets only the datasets the user may read (the current
  group's plus those shared with them), never every group's.
- **View-as** (role or user) is **admins only**. Everyone else always sees a
  dashboard through their own grants; an admin's own grants are everything.

## 3. How access is applied

One step turns a dashboard (its YAML, its group, the viewer) into what gets
rendered:

1. Every dataset reference is resolved to `(group, name)`.
2. It's checked against the viewer's grants. One the viewer can't read becomes
   the lock.
3. The grant's row filters are added to the chart's filters as **ordinary
   filters**, the same filter model as declared and crossfilters (what
   `Dashboard.as_role` does today), so they're pushed down into `scan_parquet`
   like the rest.

Every route that renders data, lists values or briefs the assistant goes
through this step, including the ones that take YAML or a dataset name from
the browser: `/execute`, `/dashboard`, `/dashboard/cell`, `/chat`,
`/chart/config/*`, `/chart/table`, `/chart/map`, `/filter/values` and
`/yaml/assist`. No route checks access on its own.

## 4. Personal groups and user names

- A user's group is `_<username>`, created with the user. An SSO user's group is
  created on their first login.
- The admin's group is always **`_admin`**, whatever `FIREFLYER_USER` is.
- **User names are immutable** (the Users dialog loses the rename) and match
  **`[a-z][a-z0-9]*`**: lowercase latin letters, digits after the first.
- An **SSO login** whose name breaks the rule (`alice.smith@corp.com`) is
  refused with a clear error, until SSO actually exists.
- **Deleting a user deletes their group** with all its datasets and dashboards.
- The `_` prefix is reserved for personal groups: no shared group may start with
  it.
- The **current group** is the user's own on login.

## 5. Groups tab

A new **Groups** tab on the list pages (admins only) adds, renames and
deletes shared groups. Personal groups are listed, locked (padlock), like the
built-in role and user: they come and go with their user.

Groups are stored in their **own table** (name, created, personal or shared).

## 6. References: `group:dataset`

- **Bare = the dashboard's group.** `dataset: orders` in a dashboard stored in
  `sales` always means `sales:orders`, whoever views it. Existing YAML stays
  valid: no `:` is used today.
- **Inline datasets** (the dashboard's `datasets:` block) shadow a **bare** name
  only, never a qualified one.
- **Normalized everywhere a name is compared**: bare inside its own group,
  qualified otherwise, so `orders` and `sales:orders` are one dataset to calcs,
  crossfilters, the uses index, permissions and the editor's pickers.
- **The chart modal** gets a **group dropdown next to the dataset** field,
  defaulting to the current group.
- **Adding:** the user picks the group before adding a dashboard or dataset
  (for a non-admin, that's only their own group).
- **Cloning into another group** qualifies each bare reference with the
  source group (`orders` becomes `sales:orders`), so the clone reads the same
  data. Only the reference is rewritten: no dataset is copied. A clone within
  the same group stays as it is.
- **Anyone who can open a dashboard can clone it** into a group they can write
  (for a non-admin, their own). Inline data comes along with the YAML.
- There's **no "move to group"** action.
- Every place that parses or rewrites a dataset name learns the qualified form:
  `rename_dataset_ref` / `rename_column_refs`, `Dashboard.dataset_names` /
  `uses`, `yaml_assist` lint and completion, the chat system prompt, the chart
  modal's dataset search and the calcs manager.

## 7. Names

- **Group and dataset names:** `[A-Za-z0-9_-]`. No `:`, no spaces.
  Case-sensitive.
- **User names:** `[a-z][a-z0-9]*` (§4).
- `_` as a first character is reserved for personal groups.
- **Dashboard names are not restricted**: emoji, Cyrillic, spaces, dots, `:`,
  even `/` and `\` are all fine. A dashboard is never referenced as
  `group:name`, and the group is its own column, never part of the name. Since
  routing decodes `%2F` back into `/` before it matches, the URL escapes first:
  `~`, `/` and `\` become `~7E`, `~2F`, `~5C`, and a leading dot `~2E`, then
  the rest is percent-encoded (`groups.url_name` / `from_url`).

## 8. Renames and deletes

- **Renaming a shared group** (admin only) rewrites every `group:` reference in
  every other group, like `rename_dataset_ref` does for datasets today.
- **Renaming a dataset or a column** cascades into dashboards in **other**
  groups too, even ones the renamer can't otherwise write. The rewritten
  dashboards get a new version, authored by the person who renamed.
- **Deleting a dataset:**
  - Used by a dashboard in **its own group**: the delete-guard refuses (409,
    listing the dashboards), as today.
  - Used only from **other groups**: allowed after a warning that lists those
    dashboards; their charts turn into locks.
- **Deleting a user:** everything in their group goes. The guard is skipped,
  and the confirm dialog lists what breaks elsewhere.
- **Deleting a shared group** works like deleting a user: it's deleted with its
  contents after a confirm that lists other groups' dashboards using its
  datasets, and those charts then show locks.

## 9. Storage, keys and migration

| today (keyed by name) | becomes |
|---|---|
| `dashboards.name` PK, `dashboard_versions.dashboard`, `dashboard_uses.dashboard` | `(group, name)` |
| `dashboard_uses (dashboard, dataset, column)` | `(group, dashboard, dataset_group, dataset, column)` |
| `datasets` catalog `name` PK; object keys `datasets/<name>/…` | `(group, name)`; `groups/<group>/datasets/<name>/…` |
| permissions: role → dataset name | role → `(group, dataset)` |
| "one role per dataset" clash check keyed by dataset name | keyed by `(group, dataset)` |
| activity log `object_name` | plus a `group` column |
| — | a `groups` table |

**Migration:**
- Every existing dashboard and dataset moves into **`_admin`**. Bare references
  stay as they are, since they're still valid inside that group.
- A dataset whose name breaks the new rule is renamed through the existing
  cascade.
- A user whose name breaks the new rule **stops the migration**, with a list of
  them, since renaming someone changes their login.

## 10. URLs and the editor

- **`/d/<group>/<name>`** for a dashboard (and its `/save`, `/versions`,
  `/clone`, …), **`/datasets/<group>/<name>`** for a dataset, likewise. The
  lists are `/d/<group>` and `/datasets/<group>`, and `/` opens your own
  group. Compatibility may break: old links aren't redirected.
- The current group comes **from the URL**, not a cookie.
- **The group dropdown** has two splits, like the view-as picker's roles |
  users switch.
- **Browser keys include the group**: `ff-draft:<group>/<name>`,
  `ff-chat:<group>/<name>`, and the `X-FF-Dashboard` header the activity log
  reads.

## 11. Lists and overviews

- Admins see everything, in every list.
- The dashboards and datasets lists get a **Group** column.
- Wherever a dependency is shown, an item from another group gets a `group:`
  prefix. That covers the dashboards list's Datasets column, the datasets
  list's Dashboards column, a dataset's per-column dashboards and the delete
  warnings.

## 12. Local paths mode

- **URLs change**: a path takes the group's place, `/d/<path>/<name>` and
  `/datasets/<path>/<name>`. The path dropdown links there and the `ff_path`
  cookie goes.
- **Nothing else changes**: paths stay isolated, and there are no users,
  personal groups or access checks.
- A **`group:` reference** (`dataset: demo:orders`) in local mode is a
  validation error, shown by the YAML lint like any other.
- The plain single-dashboard editor (no `FIREFLYER_PATHS`) is unchanged.

