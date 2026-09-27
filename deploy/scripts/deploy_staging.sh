#!/usr/bin/env bash
# Run on the pitchsidedev VM after CI passes. The frontend bundle is built
# elsewhere and carries .release-sha containing the exact commit it was built from.
set -Eeuo pipefail

APP=/home/vboxuser/EXP
WEB=/var/www/pitchside
API_URL=http://127.0.0.1:8000/health
WEB_URL=http://127.0.0.1/.release-sha
[[ $(hostname -s) == pitchsidedev ]] || { echo "This script is for pitchsidedev only" >&2; exit 2; }
for program in git rsync curl sudo systemctl; do
    command -v "$program" >/dev/null || { echo "Missing $program" >&2; exit 2; }
done

if (( $# != 2 )); then
    echo "Usage: $0 <40-character Git SHA> <prebuilt frontend/dist directory>" >&2
    exit 2
fi
sha=$1
dist=$(realpath "$2")
[[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "Provide the full 40-character Git SHA" >&2; exit 2; }
[[ -f $dist/index.html && -d $dist/assets && -f $dist/.release-sha ]] || {
    echo "Frontend bundle needs index.html, assets/, and .release-sha" >&2; exit 2;
}
# Windows text writes may use CRLF; compare the SHA after removing line endings.
[[ $(tr -d '\r\n' < "$dist/.release-sha") == "$sha" ]] || {
    echo "Frontend bundle was built from a different Git SHA" >&2; exit 2;
}
[[ -f $APP/.env && -x $APP/venv/bin/python && -d $WEB ]] || {
    echo "Expected staging .env, venv, or nginx web root is missing" >&2; exit 2;
}
[[ -z ${TEST_DATABASE_URL:-} ]] || { echo "Unset TEST_DATABASE_URL before deployment" >&2; exit 2; }

cd "$APP"
[[ -z $(git status --porcelain --untracked-files=no) ]] || {
    echo "Tracked files on the VM have local changes; resolve them first" >&2; exit 2;
}
git fetch origin main
git cat-file -e "$sha^{commit}"
git merge-base --is-ancestor "$sha" origin/main || {
    echo "Commit is not on origin/main" >&2; exit 2;
}
old_sha=$(git rev-parse HEAD)
sudo -v
for service in pitchside-api pitchside-worker pitchside-beat; do
    systemctl is-active --quiet "$service" || {
        echo "$service is not active; inspect the VM before deploying" >&2; exit 2;
    }
done

backup=$(mktemp -d /tmp/pitchside-web-XXXXXX)
sudo cp -a "$WEB/." "$backup/"
echo "Previous code: $old_sha; previous frontend: $backup"

stopped=0
restore_services() {
    if (( stopped )); then
        echo "Deployment stopped; attempting to restore the staging services" >&2
        sudo systemctl start pitchside-api pitchside-worker pitchside-beat || true
    fi
}
trap restore_services EXIT

# The VM runs Beat separately. Stop it before the worker, and stop the API
# before changing its files or dependencies. Staging accepts this downtime.
stopped=1
sudo systemctl stop pitchside-beat pitchside-worker pitchside-api
git switch --detach "$sha"
"$APP/venv/bin/python" -m pip install -r requirements.txt
"$APP/venv/bin/python" -m alembic upgrade heads

# Publish only the prebuilt frontend. Keep the old files in the backup above.
sudo rsync -a --delete "$dist/" "$WEB/"
sudo systemctl start pitchside-api
for attempt in {1..15}; do
    if curl --fail --silent --output /dev/null "$API_URL"; then break; fi
    sleep 2
done
curl --fail --silent --show-error "$API_URL" >/dev/null
served_sha=$(curl --fail --silent --show-error "$WEB_URL" | tr -d '\r\n')
[[ $served_sha == "$sha" ]] || { echo "nginx serves $served_sha, expected $sha" >&2; exit 1; }

sudo systemctl start pitchside-worker pitchside-beat
for service in pitchside-api pitchside-worker pitchside-beat; do
    systemctl is-active --quiet "$service" || { echo "$service failed to start" >&2; exit 1; }
done
stopped=0
trap - EXIT
echo "Staging deployed $sha; retained previous frontend at $backup"
