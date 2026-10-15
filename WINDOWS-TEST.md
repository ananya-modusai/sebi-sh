# Windows workstation test

Both containers on ONE machine, Docker Desktop, to prove the images work before the
VMs exist. This is **not** the VM procedure — `deploy-db.sh` / `deploy-app.sh` are for
RHEL + podman and will not run here.

Use **PowerShell** or **Git Bash**. Commands below are single-line so they paste cleanly
into either.

---

## 1. Pull both images  (~4 GB, do this first)

```
docker pull olamzar/modus-db:1.3
docker pull olamzar/modus-app:2.4
```

Already proven to work on this machine (12 Oct). `2.4`, not 2.3 — 2.3 401s on admin routes.

---

## 2. Network, so the app can find the database by name

```
docker network create modus
```

If it says it already exists, fine.

---

## 3. Database

```
docker volume create modus_pgdata
docker run -d --name modus-db --network modus -p 5432:5432 -v modus_pgdata:/var/lib/postgresql/data olamzar/modus-db:1.3
```

No `:Z` — that is SELinux, Linux only. Docker Desktop errors on it.

**Wait for it to finish loading 175 MB.** Watch until you see
`database system is ready to accept connections`:

```
docker logs -f modus-db
```

Ctrl-C to stop watching. Then confirm the data is really there:

```
docker exec modus-db psql -U modus -d surveillance -tAc "SELECT count(*) FROM surveillance_sebi.companies"
```

**Expect `247`.** A `0` or an error means init is still running — wait and retry.

---

## 4. Application

```
docker volume create modus_blobs
docker run -d --name modus-app --network modus -p 3000:8080 -v modus_blobs:/data/seaweed -e DB_HOST=modus-db -e DB_PORT=5432 -e DB_NAME=surveillance -e DB_USER=modus -e DB_PASSWORD=modus -e SSH_TUNNEL_ENABLED=false -e DEPLOY_ENV=onprem -e SURVEILLANCE_SCHEMA=surveillance_sebi -e BLOB_PUBLIC_URL=http://localhost:3000 -e REPORT_APP_URL=http://localhost:8080/ -e SEBI_ADMIN_EMAIL=ananya@modussecure.com olamzar/modus-app:2.4
```

`BLOB_PUBLIC_URL=http://localhost:3000` is correct **only because you will browse from
this same machine**. If anyone else opens it over the network, images 403 — see
IMPORTANT.md §3.1.

`DB_HOST=modus-db` is the container name; Docker's internal DNS resolves it.

Wait for startup:

```
docker logs modus-app | findstr "Application startup complete"
```

(Git Bash: `docker logs modus-app | grep "Application startup complete"`)

---

## 5. The five checks that matter

```
curl -s -o nul -w "UI    %{http_code}\n"  http://localhost:3000/
curl -s -o nul -w "API   %{http_code}\n"  http://localhost:3000/docs
curl -s -o nul -w "DATA  %{http_code}\n"  http://localhost:3000/api/companies
```

(Git Bash: `-o /dev/null` instead of `-o nul`)

| Check | Expect | If wrong |
|---|---|---|
| UI | 200 | Next.js did not start — `docker logs modus-app` |
| API | 200 | FastAPI did not start — same |
| DATA | 401 | **401 is correct here** — it means the API is up and auth is working |

Then in a browser: **http://localhost:3000**

1. Sign in with the account and password issued separately (not in this repo)
2. A company list appears — this proves the app reached the database
3. Open a company, go to posts, **check that evidence screenshots render**
4. Open the network graph tab
5. Open the Admin / Users screen — this is what 2.4 fixed, 2.3 returned 401 here

Item 3 is the one that catches the single most common deployment failure. If text loads
but images are broken, `BLOB_PUBLIC_URL` is wrong.

---

## 6. When something breaks

This machine has internet and a clipboard, so **just copy the text and send it**:

```
docker ps -a
docker logs --tail 50 modus-app
docker logs --tail 50 modus-db
```

No pastebin needed here — that problem only exists on the air-gapped VMs.

---

## 7. Cleaning up

```
docker rm -f modus-app modus-db
```

**The volumes survive**, so a re-run reuses the loaded database instead of spending five
minutes on it again. To wipe properly:

```
docker volume rm modus_pgdata modus_blobs
```

Careful: dropping `modus_pgdata` without also dropping `modus_blobs` leaves
`ingest_blobs` rows pointing at files that still exist, and vice versa — see
IMPORTANT.md §3.2.
