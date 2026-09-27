# Test VM deployment

`deploy/scripts/deploy_staging.sh` targets the existing `pitchsidedev` VM:
`/home/vboxuser/EXP`, the three separate `pitchside-*` services, and nginx's
`/var/www/pitchside` root. It does not use the `fpl-*` services described in
`docs/DEPLOY_PLAN.md`. Run it manually after the selected commit passes CI.

The script takes an exact full Git SHA and a frontend bundle built **from that
same SHA**. It checks both, refuses tracked VM changes, fetches `origin/main`,
backs up the current web root, stops Beat/worker/API, installs pinned Python
requirements, applies migrations, publishes the frontend, starts the services,
and checks the API plus nginx's served revision. It leaves the database's
`.env` on the VM and never copies it. The script causes staging downtime.

## Prepare the frontend on a machine with enough memory

From a clean clone at the CI-green commit (example Bash commands):

```bash
sha=$(git rev-parse HEAD)
cd frontend
npm ci
npm run build
printf '%s\n' "$sha" > dist/.release-sha
cd ..
scp -r frontend/dist "vboxuser@<VM_ADDRESS>:staging-dist-$sha"
```

Use the VM's actual SSH address in place of `<VM_ADDRESS>`. The bundle is not
committed. If you build on Windows, create `.release-sha` as a UTF-8 text file
with the full SHA and a final newline; avoid PowerShell 5's default UTF-16
encoding. Check `cat ~/staging-dist-*/.release-sha` on the VM.

## Deploy on the VM

Take a database backup using the VM's established backup procedure first.
From `/home/vboxuser/EXP` on the VM, with a clean tracked working tree:

```bash
sha=<full-40-character-CI-green-SHA>
bash deploy/scripts/deploy_staging.sh "$sha" "$HOME/staging-dist-$sha"
```

The VM user needs `sudo` rights to stop/start the three systemd services and
copy files into `/var/www/pitchside`. The script checks those requirements
before stopping services. The existing API remains behind nginx on port 8000.

If deployment fails, read the error and `journalctl -u pitchside-api -u
pitchside-worker -u pitchside-beat -n 100`. The script attempts to restart
stopped services, prints the previous SHA and preserves a copy of the old web
root under `/tmp/pitchside-web-*`. **Do not automatically downgrade the
database or reset code after a successful migration:** first check whether
the older code can use the new schema. A database restore needs its own
deliberate decision and the pre-deploy backup.

Once this manual deployment has succeeded, the next stage is choosing how
GitHub delivers the bundle and invokes the script. GitHub-hosted runners
cannot SSH into a LAN-only VM without a network path.
