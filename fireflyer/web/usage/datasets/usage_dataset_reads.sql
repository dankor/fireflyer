-- Which dashboards read which datasets and columns
select group_name, dashboard, dataset_group, dataset, column_name
from dashboard_uses
where dataset <> ''
