# Final Project — Microservices on EC2 + S3/CloudFront Frontend, with CI/CD

**Course:** AWS Cloud Computing
**Format:** unlike every other exercise in this course, **no setup script is provided**. You build the entire infrastructure yourselves via AWS CLI, using everything from Lessons 1-5. This document is the spec — the topology, the per-workload requirements, and what the CI/CD pipeline is expected to do. All three application workloads are provided and ready to deploy as-is — you are not expected to write application code, only infrastructure and pipeline.

---

## Topology

**Frontend delivery path** (independent of the VPC below — the browser only touches the VPC when it calls the ALB directly for `/visits` and `/stats`):
```
                                         Internet
                                            │
                                            ▼
                                     CloudFront (CDN)
                                            │
                                            ▼
                            S3 bucket — frontend static files
                      (private bucket, no public access — reachable
                       only via CloudFront's Origin Access Control)
```

**Application path:**
```
                              Internet
                                 │
                                 ▼
                         Internet Gateway
                                 │
                                 ▼
┌─────────────────────────────VPC──────────────────────────────┐
│  Public Subnets (2 AZs)                                       │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                          ALB                            │  │
│  │                /visits*  ──▶  visits-tg                 │  │
│  │                /stats*   ──▶  stats-tg                  │  │
│  └────────────────────────────────────────────────────────┘  │
│               │                               │               │
│               ▼                               ▼               │
│                                                                │
│  Private Subnets (2 AZs)                                      │
│                                                                │
│  ┌────────────────────────┐      ┌────────────────────────┐  │
│  │     visits-service      │      │     stats-service       │  │
│  │     EC2, port 8080      │      │     EC2, port 8081      │  │
│  │  pulls image from ECR   │      │  pulls image from ECR   │  │
│  └────────────────────────┘      └────────────────────────┘  │
│               │                                                │
│               ▼                                                │
│  ┌────────────────────────┐                                   │
│  │     RDS PostgreSQL      │                                   │
│  │  (private subnet only)  │                                   │
│  └────────────────────────┘                                   │
└─────────────────────────────────────────────────────────────────┘
```
`stats-service` also calls `visits-service` directly over its **private IP** on port 8080 — that call never goes through the ALB, it's plain instance-to-instance traffic inside the VPC (left out of the boxes above to keep them readable — see the security group requirement below).

ECR: two repositories — `final-project-visits-service`, `final-project-stats-service` (the frontend has no ECR repo — it's static files, not a container). Each EC2 instance: SSM access only, no SSH, no inbound port 22 anywhere.

**Two microservices, not one:** `visits-service` owns the database — it's the only thing with RDS credentials, and the only thing allowed through the RDS security group. `stats-service` never touches RDS directly; it calls `visits-service`'s **private IP** internally (inside the VPC, never through the ALB) and computes a derived stat on top. This is a real microservices boundary: if `stats-service` wanted its own database access instead, that would mean two services owning the same data — the kind of coupling this pattern exists to avoid.

**Why the frontend needs CORS (and the previous draft of this project didn't):** the frontend is served from a **CloudFront domain**, and the two microservices sit behind an **ALB on a different domain**. That's a genuine cross-origin request — the browser will block it unless `visits-service` and `stats-service` explicitly send `Access-Control-Allow-Origin` headers back. Both workloads already have CORS middleware wired in (see `ALLOWED_ORIGIN` below) — you just need to set it correctly.

---

## Requirements — Where Each Workload Deploys

### `workloads/visits-service/`
- **Deploys to:** the `visits-service EC2` instance
- **Listens on:** port `8080`
- **Health check path:** `GET /health` — bypasses ALB routing rules entirely (target group health checks hit the target directly), does not touch the database
- **Routes:** `POST /visits` (increments + returns the count — called by the frontend), `GET /visits/count` (read-only — called internally by `stats-service`; never let it call `POST /visits` or every stats refresh silently inflates the counter)
- **Required environment variables:** `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (your RDS endpoint/credentials), `ALLOWED_ORIGIN` (your CloudFront distribution's domain, e.g. `https://d123abc.cloudfront.net` — not `*`, since this service talks to a real database and shouldn't accept requests from arbitrary origins)
- **IAM on its instance role:** `AmazonSSMManagedInstanceCore` + `AmazonEC2ContainerRegistryReadOnly`
- **Security group:** inbound 8080 from the ALB's SG **and** from `stats-service`'s SG (the internal call); it is the only thing allowed to reach RDS on 5432

### `workloads/stats-service/`
- **Deploys to:** the `stats-service EC2` instance
- **Listens on:** port `8081`
- **Health check path:** `GET /health`
- **Route:** `GET /stats` — calls `visits-service` internally and returns a combined result
- **Required environment variables:** `VISITS_SERVICE_URL` (e.g. `http://<visits-service-private-ip>:8080` — its private IP, not the ALB), `ALLOWED_ORIGIN` (same CloudFront domain)
- **IAM on its instance role:** same as `visits-service`
- **Security group:** inbound 8081 from the ALB's SG only. It also calls `visits-service` on 8080 — but leave outbound at its default (allow-all) rather than trying to scope it down. Restricting egress to just `visits-service`'s SG sounds like the "more secure" move, but it silently breaks SSM and `docker pull` from ECR too, since both need outbound 443 to AWS's API endpoints — the default allow-all already covers the internal call to `visits-service`, so there's nothing to gain by narrowing it here.

### `workloads/frontend/`
- **Deploys to:** S3 (static hosting, bucket **not** public — access only via CloudFront using Origin Access Control) + CloudFront in front of it
- **No Dockerfile, no ECR repo, no EC2 instance** — this is the one workload in the project that isn't a container
- **Build-time requirement:** `index.html` contains the literal placeholder `__API_BASE_URL__` — your deploy step must replace it with the ALB's public DNS name (e.g. `sed -i "s|__API_BASE_URL__|http://your-alb-dns|" index.html`) **before** uploading to S3, since static hosting has no runtime environment variables
- **Calls:** `POST {API_BASE}/visits` and `GET {API_BASE}/stats` — both against the same ALB origin, distinguished by path, not by port

### Database
- **RDS PostgreSQL**, private subnet(s) only, `--no-publicly-accessible`
- **Security group:** inbound 5432 from `visits-service`'s security group only

---

## What We Expect From the CI/CD Pipeline

Build this as a GitHub Actions workflow (`.github/workflows/deploy.yml`), following the same OIDC pattern from Lesson 5 — **no AWS access keys stored in GitHub at any point**.

On every push to `main`, the pipeline is expected to:

1. **Authenticate to AWS via OIDC** (`AssumeRoleWithWebIdentity`) — one IAM role, trust policy scoped to this repo + `main` branch only
2. **Detect which workload(s) changed** (`workloads/visits-service/`, `workloads/stats-service/`, `workloads/frontend/` — three independent triggers, not one). A commit touching only one should never rebuild or redeploy the other two.
3. **For a changed microservice** (`visits-service` or `stats-service`):
   - Build its Docker image, push to **its own ECR repository**
   - Deploy via SSM Run Command to **its own EC2 instance** (`docker pull` + stop old + run new) — no SSH
4. **For a changed frontend:**
   - Inject the ALB's DNS name into `index.html` (replace `__API_BASE_URL__`)
   - Sync the result to the S3 bucket (`aws s3 sync`)
   - Create a CloudFront invalidation (`/*`) — otherwise the old cached file keeps serving after deploy, which is a real, easy-to-miss gotcha with CloudFront + S3 static sites
5. **If nothing relevant changed** (e.g. a docs-only commit), the pipeline should still run but touch nothing

**Verification you'll be asked to demonstrate:**
- A commit touching only `workloads/stats-service/` redeploys **only** the stats-service instance — confirm `visits-service`'s container start time is unchanged
- A frontend-only commit shows up after a hard refresh **without** redeploying either microservice — and you can explain why the CloudFront invalidation step was necessary to see it at all
- `iam simulate-principal-policy` confirms the GitHub Actions role can act on this project's resources and is denied on everything else (mirror the verification method from Lesson 5)

---

## Cleanup

No cleanup script is provided either. Tear down everything you built, in dependency order: CloudFront distribution (must be disabled before deletion — this takes time, start it first) → S3 bucket contents + bucket → EC2 instances → ALB + target groups → RDS instance → security groups → NAT Gateway + EIP → subnets/route tables/VPC → IAM roles + ECR repositories. Finish with a fresh `describe-*`/`list-*` sweep across every service you touched to confirm nothing billable is left running.
