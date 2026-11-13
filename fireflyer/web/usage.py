"""Usage analytics, shipped with the portal: a connection to Fireflyer's own
database (`internal`), datasets over it and a **Fireflyer Usage** dashboard on
them, in the admin's group.

They're made **once**, when the portal starts and the admin's group has none of
the datasets (`app._set_up_usage`). After that they're ordinary: an admin
edits or removes the connection, edits the datasets' SQL and refreshes them by
hand, and an upgrade never changes them.

The SQL gives usernames, never other personal data: no email or names, no IP
address or browser, no password hash or session key.
"""

from __future__ import annotations

CONNECTION = "internal"
CONNECTION_DESCRIPTION = "Fireflyer's own database: who uses what"

# name -> (description, SQL). `dataset_versions` is the table a connection to
# Fireflyer's own database adds, parsed from the datasets' documents.
DATASETS: dict[str, tuple[str, str]] = {
    "usage_activity": (
        "What people did: each action, who did it and when",
        """\
select at::timestamptz as at, actor, action,
       coalesce(nullif(object_type, ''), action) as object_type,  -- a login is on nothing
       object_name, group_name, status_code, status_code >= 400 as failed,
       detail::jsonb ->> 'error' as error  -- why it failed, in the words it was refused with
from activity""",
    ),
    "usage_dashboards": (
        "Every dashboard: its saves, and how often it was opened",
        """\
select d.group_name, d.name as dashboard, d.author,
       d.created_at::timestamptz as created_at, d.updated_at::timestamptz as updated_at,
       (select count(*) from dashboard_versions v
         where v.group_name = d.group_name and v.dashboard = d.name) as saves,
       (select count(*) from activity a
         where a.action = 'open' and a.object_type = 'dashboard'
           and a.group_name = d.group_name and a.object_name = d.name) as opens
from dashboards d""",
    ),
    "usage_dataset_reads": (
        "Which dashboards read which datasets and columns",
        """\
select group_name, dashboard, dataset_group, dataset, column_name
from dashboard_uses
where dataset <> ''""",
    ),
    "usage_datasets": (
        "Every dataset: versions, rows, what it keeps, and who reads it",
        """\
with uploads as (
  select group_name, dataset, split_part(version, '.', 1) as major, max(size) as size
  from dataset_versions
  group by 1, 2, 3
)
select v.group_name, v.dataset, count(*) as versions,
       coalesce(max(v.rows) filter (where v.current), 0) as rows,
       round(((select sum(u.size) from uploads u
                where u.group_name = v.group_name and u.dataset = v.dataset)
              + max(v.data_size)) / 1048576.0, 3) as stored_mb,
       max(v.created_at) as last_version,
       (select count(distinct r.group_name || '/' || r.dashboard) from dashboard_uses r
         where r.dataset_group = v.group_name and r.dataset = v.dataset) as dashboards
from dataset_versions v
group by v.group_name, v.dataset""",
    ),
    "usage_users": (
        "Every user: how they sign in and the roles they hold",
        """\
select u.name as username, u.source, u.created_at::timestamptz as created_at,
       array_remove(array_agg(r.role_name order by r.role_name), null) as roles
from users u left join user_roles r on r.user_name = u.name
group by u.name, u.source, u.created_at""",
    ),
}

DASHBOARD_NAME = "Fireflyer Usage"

# The dashboard on them. Refreshing a dataset (its page's ↻) brings it up to date.
DASHBOARD = """\
name: Fireflyer Usage
description: Who uses Fireflyer and how, from its own database. Refresh the usage_ datasets to update it.

calcs:
  # Keyed like a column, a calc only renames it - the tables' headers.
  usage_activity:
    at: {name: When, formula: at}
    actor: {name: User, formula: actor}
    action: {name: Action, formula: action}
    object_type: {name: Object, formula: object_type}
    object_name: {name: Name, formula: object_name}
    status_code: {name: Status, formula: status_code}
    error: {name: Error, formula: error}
    actions: {name: Actions, agg: count}
    people: {name: Active users, agg: dcount, formula: actor}
    opens:
      name: Dashboard opens
      agg: count
      filters: [{column: action, op: in, values: [open]}, {column: object_type, op: in, values: [dashboard]}]
    errors:
      name: Errors
      description: Actions refused or failed - a 4xx or 5xx answer
      agg: count
      filters: [{column: failed, op: in, values: ['true']}]
  usage_dashboards:
    group_name: {name: Group, formula: group_name}
    dashboard: {name: Dashboard, formula: dashboard}
    author: {name: Author, formula: author}
    opens: {name: Opens, formula: opens}
    saves: {name: Saves, formula: saves}
    updated_at: {name: Updated, formula: updated_at}
    dashboard_count: {name: Dashboards, agg: count}
    saves_total: {name: Saves, agg: sum, formula: saves}
  usage_datasets:
    group_name: {name: Group, formula: group_name}
    dataset: {name: Dataset, formula: dataset}
    dashboards: {name: Dashboards, formula: dashboards}
    versions: {name: Versions, formula: versions}
    rows: {name: Rows, formula: rows}
    stored_mb: {name: Stored (MB), formula: stored_mb}
    dataset_count: {name: Datasets, agg: count}
    stored: {name: Stored, agg: sum, formula: stored_mb, format: 0.00 MB}
  usage_users:
    user_count: {name: Users, agg: count}

charts:
  kpi_people: {type: number, dataset: usage_activity, title: Active users, calc: people}
  kpi_actions: {type: number, dataset: usage_activity, title: Actions, calc: actions}
  kpi_opens: {type: number, dataset: usage_activity, title: Dashboard opens, calc: opens}
  kpi_errors: {type: number, dataset: usage_activity, title: Errors, calc: errors}

  by_day:
    type: bar
    dataset: usage_activity
    title: Actions by day
    description: Each action people took, by what it was on
    x: at
    y: object_type
    calc: actions
  people_by_day:
    type: bar
    dataset: usage_activity
    title: Active users by day
    x: at
    calc: people
  actions_pie:
    type: pie
    dataset: usage_activity
    title: What people do
    column: action
    calc: actions
  top_dashboards:
    type: bar
    dataset: usage_activity
    title: Most opened dashboards
    x: object_name
    calc: opens
    top: 10
    direction: vertical
  top_people:
    type: bar
    dataset: usage_activity
    title: Most active users
    x: actor
    calc: actions
    top: 10
    direction: vertical
  errors:
    type: table
    dataset: usage_activity
    title: Errors
    description: Every refused or failed action, newest first, and why
    columns: [at, actor, action, object_type, object_name, status_code, error]
    sort: [-at]
    filters: [{column: failed, op: in, values: ['true']}]

  kpi_dashboards: {type: number, dataset: usage_dashboards, title: Dashboards, calc: dashboard_count}
  kpi_datasets: {type: number, dataset: usage_datasets, title: Datasets, calc: dataset_count}
  kpi_stored: {type: number, dataset: usage_datasets, title: Stored, calc: stored}
  kpi_users: {type: number, dataset: usage_users, title: Users, calc: user_count}
  dashboards:
    type: table
    dataset: usage_dashboards
    title: Dashboards
    description: Least opened first - the ones nobody looks at lead
    columns: [group_name, dashboard, author, opens, saves, updated_at]
    sort: [opens]
  datasets:
    type: table
    dataset: usage_datasets
    title: Datasets
    description: Fewest readers first - a dataset no dashboard reads leads
    columns: [group_name, dataset, dashboards, versions, rows, stored_mb]
    sort: [dashboards, -stored_mb]
  storage:
    type: pie
    dataset: usage_datasets
    title: Storage by group
    column: group_name
    calc: stored

layout:
  Activity:
    - ["@18", "kpi_people", "kpi_actions", "kpi_opens", "kpi_errors"]
    - ["@40", "by_day:65", "actions_pie:35"]
    - ["@40", "people_by_day:40", "top_dashboards:30", "top_people:30"]
    - ["@40", "errors"]
  Content:
    - ["@18", "kpi_dashboards", "kpi_datasets", "kpi_stored", "kpi_users"]
    - ["@44", "dashboards"]
    - ["@44", "datasets:62", "storage:38"]
"""
