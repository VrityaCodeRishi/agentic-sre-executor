## Postgres (incident memory for agentic workflow)

This deploys a single-node Postgres instance in the `agentic` namespace for the MVP incident store.

### Install

```bash
kubectl apply -f application/postgres/postgres.yaml
kubectl -n agentic get pods -w
```

### Connect (from your laptop via port-forward)

```bash
kubectl -n agentic port-forward svc/postgres 5432:5432
```

Then:

```bash
PGPASSWORD="sre_agent_password" psql -h 127.0.0.1 -p 5432 -U sre_agent -d sre_incidents
```

### Service DNS (in-cluster)

- Host: `postgres.agentic.svc.cluster.local`
- Port: `5432`
- DB: `sre_incidents`

### Notes

- Credentials are stored in a Kubernetes `Secret` (`postgres-auth`) using `stringData` for MVP simplicity. Rotate + harden later.
- Storage uses the cluster’s default StorageClass unless you set one explicitly.


