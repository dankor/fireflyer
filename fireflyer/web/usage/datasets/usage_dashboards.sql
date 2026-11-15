-- Every dashboard: its saves, and how often it was opened
select d.group_name, d.name as dashboard, d.author,
       d.created_at::timestamptz as created_at, d.updated_at::timestamptz as updated_at,
       (select count(*) from dashboard_versions v
         where v.group_name = d.group_name and v.dashboard = d.name) as saves,
       (select count(*) from activity a
         where a.action = 'open' and a.object_type = 'dashboard'
           and a.group_name = d.group_name and a.object_name = d.name) as opens
from dashboards d
