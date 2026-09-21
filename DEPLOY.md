# Deploying the backend to Render

The API is a normal FastAPI service, with one thing that shapes every decision
below: **it keeps state on disk.** The SQLite database and the Excel exports
are files, so the service needs a persistent disk and exactly one instance.

---

## Before you start

This backend lives in its own repository:

    https://github.com/TheHammadAli/Scraper-Backend

so Render can read it directly. `.gitignore` keeps `data/`, `exports/`,
`logs/`, `.cache/` and `.venv/` out — check `git status` before committing so
no collected data or secrets go up.

---

## Option A — Blueprint (the quick way)

`backend/render.yaml` describes the whole service.

1. Render dashboard → **New** → **Blueprint**
2. Connect the GitHub repo
3. Render reads `render.yaml` and shows what it will create — a web service
   with a 1 GB disk
4. It will ask for `CORS_ORIGINS` (marked `sync: false`). Put the frontend's
   URL, or `*` for now and tighten it later
5. **Apply**

---

## Option B — By hand

Render dashboard → **New** → **Web Service** → connect the repo, then:

| Field | Value |
|---|---|
| Root Directory | *(leave blank — the backend is the repo root)* |
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Instance Type | **Starter** or higher (not Free — see below) |
| Health Check Path | `/api/health` |

### Add the disk

Service → **Disks** → **Add Disk**

| Field | Value |
|---|---|
| Name | `collector-data` |
| Mount Path | `/var/data` |
| Size | 1 GB |

### Environment variables

Service → **Environment**

| Key | Value |
|---|---|
| `PYTHON_VERSION` | `3.11.9` |
| `DATABASE_PATH` | `/var/data/listings.db` |
| `EXPORT_DIR` | `/var/data/exports` |
| `CACHE_DIR` | `/var/data/cache` |
| `CORS_ORIGINS` | your frontend URL, e.g. `https://collector.vercel.app` |

`PORT` is set by Render; don't add it yourself.

---

## Three things that will bite you

**1. The Free plan loses your data.** Free instances have no disk and sleep
after 15 minutes of inactivity. On wake, the filesystem is fresh — the
database is gone, and a scrape that was running is killed. Use Starter or
above.

**2. Keep it at one instance.** A collection runs as a background thread
inside the web process. Two instances means two runs at once hitting the same
sites, and each would have its own idea of what is already stored. `render.yaml`
pins `numInstances: 1`; if you scale by hand, don't.

**3. `DATABASE_PATH` must point at the mounted disk.** Every deploy replaces
the checkout. If the database sits inside it, each deploy wipes your collected
data. That is what `/var/data/...` is for.

---

## After it deploys

```bash
curl https://<your-service>.onrender.com/api/health
# {"status":"ok","active_job":null}
```

Then sync the OLX categories once — the deployed instance starts with an empty
database and no category cache. Render → your service → **Shell**:

```bash
python run.py sync-categories
```

### Point the frontend at it

In the frontend's host (Vercel, Netlify, wherever):

```bash
NEXT_PUBLIC_API_URL=https://<your-service>.onrender.com
```

And set `CORS_ORIGINS` on Render to that frontend's URL. Both sides have to
agree or the browser blocks every request.

---

## One honest caveat

Render runs in a datacenter, and classified sites treat datacenter IPs more
suspiciously than home connections. Scrapes that work fine from your laptop
may hit rate limits or blocks from Render.

If that happens, don't reach for faster settings — that makes it worse. Check
`run.py verify` from the Render shell first to see whether pages still parse.
Running collections from a machine you control, and deploying only the UI and
API, is the more reliable arrangement.

---

## Scheduled runs

Render Cron Jobs run in their own container, so a cron job cannot see the web
service's disk. To collect on a schedule, either:

- use the web service's shell / an API call to start a job, or
- run the CLI (`python run.py run ...`) on a machine that has the database

Two services writing to one SQLite file is not something to attempt.
