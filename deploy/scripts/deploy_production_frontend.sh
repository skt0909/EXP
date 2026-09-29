#!/usr/bin/env bash
# Manually promote a CI-green, frontend-only commit on ps-vps.
set -Eeuo pipefail

APP=/home/saket/EXP
WEB=$APP/frontend/dist
BACKUPS=/home/saket/pitchside-web-backups
HOST=http://127.0.0.1

[[ $(hostname -s) == ps-vps ]] || { echo "This script is for ps-vps only" >&2; exit 2; }
[[ $# == 1 && $1 =~ ^[0-9a-f]{40}$ ]] || {
    echo "Usage: $0 <full 40-character CI-green Git SHA>" >&2; exit 2;
}
sha=$1
for program in git npm curl cp; do
    command -v "$program" >/dev/null || { echo "Missing $program" >&2; exit 2; }
done
[[ -f $APP/.env && -d $WEB && -x $APP/venv/bin/python ]] || {
    echo "Production repo, environment, venv, or web root is missing" >&2; exit 2;
}
for service in fpl-api fpl-worker fpl-beat; do
    systemctl is-active --quiet "$service" || { echo "$service is not active" >&2; exit 2; }
done
# The production config must let nginx serve /docs/User%20Guide.pdf itself.
grep -Eq 'location[[:space:]]+=[[:space:]]+/docs[[:space:]]*\{' /etc/nginx/sites-enabled/fpl || {
    echo "The production nginx /docs route is not the verified exact-match configuration" >&2; exit 2;
}

cd "$APP"
[[ -z $(git status --porcelain --untracked-files=no) ]] || {
    echo "Tracked files have local changes; resolve them before deploying" >&2; exit 2;
}
git fetch origin main
git cat-file -e "$sha^{commit}"
git merge-base --is-ancestor "$sha" origin/main || {
    echo "Requested commit is not on origin/main" >&2; exit 2;
}
old_sha=$(git rev-parse HEAD)
git merge-base --is-ancestor "$old_sha" "$sha" || {
    echo "Requested commit is older than or diverges from the deployed code" >&2; exit 2;
}

# This path deliberately refuses backend code, dependencies, migrations and
# systemd/nginx changes. Such releases need a separately reviewed procedure.
while IFS= read -r path; do
    case "$path" in
        frontend/*|docs/*|README.md|.gitattributes|.github/*|backend/Tests/*|deploy/STAGING.md|deploy/PRODUCTION.md|deploy/scripts/*) ;;
        *) echo "Not a frontend-only release: $path" >&2; exit 2 ;;
    esac
done < <(git diff --name-only "$old_sha" "$sha")

mkdir -p "$BACKUPS"
backup=$(mktemp -d "$BACKUPS/web-XXXXXX")
cp -a "$WEB/." "$backup/"
build=$(mktemp -d "$HOME/pitchside-build-XXXXXX")
echo "Previous commit: $old_sha; previous frontend: $backup"

success=0
recover() {
    if (( !success )); then
        echo "Deploy failed; restoring the previous frontend and Git commit" >&2
        cp -a "$backup/." "$WEB/" || true
        if [[ ! -e $backup/.release-sha ]]; then rm -f "$WEB/.release-sha"; fi
        git -C "$APP" switch --detach "$old_sha" || true
    fi
}
trap recover EXIT

git switch --detach "$sha"
cd "$APP/frontend"
npm ci
npm run build -- --outDir "$build"
[[ -s $build/index.html && -d $build/assets && -s $build/docs/'User Guide.pdf' ]] || {
    echo "The frontend build or user guide is missing" >&2; exit 1;
}
printf '%s\n' "$sha" > "$build/.release-sha"

# Keep old hashed assets for visitors with an already-open page. Copy index
# last via rename so nginx never reads a partially written index.html.
for entry in "$build"/* "$build"/.[!.]* "$build"/..?*; do
    [[ -e $entry ]] || continue
    [[ $(basename "$entry") == index.html ]] && continue
    cp -a "$entry" "$WEB/"
done
cp "$build/index.html" "$WEB/.index.html.new"
mv -f "$WEB/.index.html.new" "$WEB/index.html"

[[ $(curl -fsS "$HOST/.release-sha" | tr -d '\r\n') == "$sha" ]] || {
    echo "nginx is not serving the requested revision" >&2; exit 1;
}
curl -fsSI "$HOST/docs" >/dev/null
curl -fsSI "$HOST/docs/User%20Guide.pdf" | tr -d '\r' | grep -qi '^Content-Type: application/pdf' || {
    echo "nginx is not serving the user guide as a PDF" >&2; exit 1;
}
curl -fsS http://127.0.0.1:8000/health >/dev/null
for service in fpl-api fpl-worker fpl-beat; do
    systemctl is-active --quiet "$service" || { echo "$service stopped" >&2; exit 1; }
done
success=1
trap - EXIT
echo "Production frontend deployed $sha; previous frontend retained at $backup"
