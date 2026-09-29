# Manual production frontend releases

`deploy/scripts/deploy_production_frontend.sh` targets the existing `ps-vps`
layout: `/home/saket/EXP`, nginx serving `frontend/dist`, and the three
`fpl-api`, `fpl-worker`, and `fpl-beat` services. It runs on the VPS as `saket`
after the chosen Git SHA has passed CI and staging has been checked.

For the first use, the currently deployed checkout will not yet contain this
script. Fetch without switching the running checkout, then copy just the
script into the home directory and check its syntax:

```bash
cd /home/saket/EXP
sha=<full-40-character-Git-SHA>
git fetch origin main
git show "$sha":deploy/scripts/deploy_production_frontend.sh > ~/deploy_production_frontend.sh
bash -n ~/deploy_production_frontend.sh
bash ~/deploy_production_frontend.sh "$sha"
```

For subsequent releases, the tracked script can be invoked from the repo:

```bash
bash deploy/scripts/deploy_production_frontend.sh <full-40-character-Git-SHA>
```

The script fetches `origin/main`, checks that the requested SHA is on it and
descends from the deployed commit, and refuses changes outside frontend,
tests, documentation, workflows and deployment scripts. It leaves the
production database and services untouched. It backs up the served files
under `/home/saket/pitchside-web-backups/`, checks out the exact commit,
installs the frontend lockfile with `npm ci`, builds into a separate temporary
directory, publishes the files, and checks nginx, the API and the services.
An error attempts to restore the previous frontend and Git commit. The old
hashed assets remain in the web root so open pages can finish loading.

The nginx site needs an **exact** `location = /docs` proxy route so the guide
at `/docs/User%20Guide.pdf` falls through to static-file serving. On the
current VPS that rule was applied and checked during the `f260361` release.
Keep its backup at `/home/saket/fpl-nginx-before-guide.conf` for reference.

The VM has 1 GB RAM plus swap. The first manual build of this frontend on it
completed in about 3 seconds; monitor capacity on future releases. This
script intentionally refuses backend code, Python dependency, migration,
nginx, or systemd changes. Those need a separately reviewed production
procedure and a database backup before any migration.
