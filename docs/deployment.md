# Deploy the portal

This page tells how to run the Fireflyer portal for a team: on a server or a
cluster, behind a proxy that does HTTPS, with PostgreSQL and S3 storage that
the platform gives. For local development, use `docker compose --profile
portal up` (see the [README](../README.md)).

## The image

Build the image from the repository:

```bash
docker build -t fireflyer .
```

The default stage, `portal`, has the portal and its drivers. It has no test
tools. It runs as user `10001`, and its command is `python -m fireflyer.portal`:
one process, with no reloader. The app writes only to `/app/data` and `/tmp`.

Run **one replica** and stop the old pod before the new one starts (in
Kubernetes, the `Recreate` strategy). The builds of datasets are kept in the
memory of the process. Two processes can start two builds of one dataset.

## Settings

Give the settings as environment variables.

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | The PostgreSQL database, with no password: `postgresql://fireflyer@db:5432/fireflyer?sslmode=require`. Required. |
| `PGPASSWORD` | The password of the database user. |
| `FIREFLYER_PASSWORD` | The password of the admin login. Required. |
| `FIREFLYER_SECRET` | A long random value that signs the sessions. Required. Keep it the same across restarts: a new value signs everyone out. |
| `FIREFLYER_USER` | The name of the admin login. The default is `admin`. |
| `FIREFLYER_S3_ENDPOINT` | The S3 endpoint, with its scheme: `https://s3.example.com` or `http://s3.internal:9000`. |
| `FIREFLYER_S3_BUCKET` | The bucket. The default is `fireflyer`. |
| `FIREFLYER_S3_ACCESS_KEY`, `FIREFLYER_S3_SECRET_KEY` | The key of the bucket. |
| `FIREFLYER_S3_REGION` | The region, for example `us-east-1`. |
| `FIREFLYER_TLS` | `off` behind a proxy that does HTTPS. |
| `FIREFLYER_FORWARDED_ALLOW_IPS` | The addresses of the proxy, or `*` if only the proxy can reach the pod. Then the app sees the requests as HTTPS, and the session cookie is `Secure`. |
| `FIREFLYER_PORT` | The port. The default is `8000`. |
| `FIREFLYER_CONNECTION_PASSWORDS` | The variables that a connection can take its password from, with commas between them. See [Connections](#connections). |
| `ANTHROPIC_API_KEY` | The key for the AI assistant. Optional. |

If `FIREFLYER_PASSWORD` or `FIREFLYER_SECRET` is not set, the portal does not
start. Their defaults are public.

## The database

The portal keeps dashboards, users, roles, the catalog of datasets and the
activity log in PostgreSQL.

- **The user needs schema rights.** At start, the portal makes and changes its
  tables. Give its user the right to do this in its own database.
- **Keep the password out of the URL.** Set it in `PGPASSWORD`. At start, the
  portal removes `PGPASSWORD` from its environment and keeps it in memory, so
  that no other database connection can send it to a different server. A
  password in `DATABASE_URL` also works.
- **Other libpq variables work.** `PGHOST`, `PGPORT`, `PGUSER`, `PGSSLMODE`,
  `PGSSLROOTCERT` and the others give what `DATABASE_URL` leaves out.
  `DATABASE_URL=postgresql://` takes everything from them. A `.pgpass` file is
  not read.
- **The portal connects again by itself.** If the database restarts, the next
  request opens a new connection. A credential rotation needs a restart of the
  pod.

## Dataset storage

The portal keeps the uploads and the built Parquet files of the datasets in an
S3 bucket. Make the bucket before the first start. The portal uses path-style
addresses (`endpoint/bucket/key`), so S3-compatible stores such as Garage and
MinIO work. If the endpoint is `http://`, the portal also reads it over plain
HTTP. An `https://` endpoint keeps its TLS checks.

Without `FIREFLYER_S3_ENDPOINT`, the datasets go in `/app/data/datasets`. Use
this only with a persistent volume.

## Health probes

Both probes answer without a login.

| Path | Answer |
| --- | --- |
| `/healthz` | 200 while the process answers. It does not ask the database or the storage. Use it for liveness and start-up. |
| `/ready` | 200 if the database and the bucket answer within 3 seconds, else 503. Use it for readiness. |

`/ready` opens a new database connection and runs `select 1`. Then it asks for
the head of its own bucket. It does not list buckets and it does not read or
write a dataset. The answer does not tell why a check failed. Set the timeout of
the readiness probe to 5 seconds or more.

Do not use `/` or `/login` as a probe. They answer when the database is down.

## Connections

An admin can add connections to other databases. A connection gives the name
of the variable that holds its password. Fireflyer stores no password.

1. Put the password in a variable of the pod, for example `WAREHOUSE_PASSWORD`.
2. Add its name to `FIREFLYER_CONNECTION_PASSWORDS`.
3. Let the pod reach the host of the database.

The `internal` connection uses the database of the portal and `PGPASSWORD`. An
admin of Fireflyer can use each allowed password, and can point a connection to
any host that the pod can reach. Limit the outbound network of the pod to the
databases that you use. For the usage analytics, you can give `internal` a
read-only user: change its URL and its password variable on the Connections
tab.

## Upgrades

At start, the portal changes its schema when a new version needs it. An older
image cannot always open a schema that a newer one changed. For example,
0.29.0 removes the stored connection passwords. Before an upgrade, back up the
database and the bucket together. Their contents must agree.

## Example

A Kubernetes container, behind an ingress that does HTTPS:

```yaml
containers:
  - name: fireflyer
    image: registry.example.com/fireflyer:0.30.0
    ports: [{containerPort: 8000}]
    env:
      - {name: DATABASE_URL, value: "postgresql://fireflyer@db:5432/fireflyer?sslmode=require"}
      - {name: PGPASSWORD, valueFrom: {secretKeyRef: {name: fireflyer-db, key: password}}}
      - {name: FIREFLYER_PASSWORD, valueFrom: {secretKeyRef: {name: fireflyer, key: admin-password}}}
      - {name: FIREFLYER_SECRET, valueFrom: {secretKeyRef: {name: fireflyer, key: session-secret}}}
      - {name: FIREFLYER_S3_ENDPOINT, value: "http://s3.internal:9000"}
      - {name: FIREFLYER_S3_BUCKET, value: "fireflyer"}
      - {name: FIREFLYER_S3_REGION, value: "us-east-1"}
      - {name: FIREFLYER_S3_ACCESS_KEY, valueFrom: {secretKeyRef: {name: fireflyer-s3, key: access-key}}}
      - {name: FIREFLYER_S3_SECRET_KEY, valueFrom: {secretKeyRef: {name: fireflyer-s3, key: secret-key}}}
      - {name: FIREFLYER_TLS, value: "off"}
      - {name: FIREFLYER_FORWARDED_ALLOW_IPS, value: "*"}
    livenessProbe: {httpGet: {path: /healthz, port: 8000}}
    readinessProbe: {httpGet: {path: /ready, port: 8000}, timeoutSeconds: 5}
    startupProbe: {httpGet: {path: /healthz, port: 8000}, periodSeconds: 5, failureThreshold: 60}
```

Use the `Recreate` strategy and one replica.
