#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${STATE_ENCRYPTION_KEY:?STATE_ENCRYPTION_KEY is required for encrypted backups}"

tracker_repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
state_tag="${STATE_RELEASE_TAG:-tracker-state}"
state_db="${STATE_DB:-var/ot-tracker.sqlite3}"
retention_days="${STATE_BACKUP_RETENTION_DAYS:-7}"
runner_tmp="${RUNNER_TEMP:-/tmp}"
backup_dir="$(mktemp -d "${runner_tmp}/ot-tracker-backup.XXXXXX")"
trap 'rm -rf -- "${backup_dir}"' EXIT

if [[ ! -f "${state_db}" ]]; then
  echo "Tracker database does not exist: ${state_db}"
  exit 1
fi
if [[ ! "${retention_days}" =~ ^[1-9][0-9]*$ ]]; then
  echo "STATE_BACKUP_RETENTION_DAYS must be a positive integer."
  exit 1
fi

snapshot_db="${backup_dir}/snapshot.sqlite3"
python3 - "${state_db}" "${snapshot_db}" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
    result = destination.execute("PRAGMA quick_check").fetchone()
finally:
    destination.close()
    source.close()
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY

backup_date="$(date -u +%F)"
stable_name="ot-tracker.sqlite3.gz.gpg"
daily_name="ot-tracker-${backup_date}.sqlite3.gz.gpg"
install -d -m 700 "${backup_dir}/gnupg"
gzip --no-name --stdout "${snapshot_db}" | gpg \
  --homedir "${backup_dir}/gnupg" \
  --batch --no-tty --pinentry-mode loopback --no-symkey-cache \
  --passphrase-fd 3 --symmetric --cipher-algo AES256 --compress-algo none \
  --output "${backup_dir}/${stable_name}" 3<<<"${STATE_ENCRYPTION_KEY}"
cp "${backup_dir}/${stable_name}" "${backup_dir}/${daily_name}"
(
  cd "${backup_dir}"
  sha256sum "${stable_name}" >"${stable_name}.sha256"
  sha256sum "${daily_name}" >"${daily_name}.sha256"
)

if ! release_lookup="$(gh api "repos/${tracker_repo}/releases/tags/${state_tag}" --jq '.id' 2>&1)"; then
  if [[ "${release_lookup}" != *"HTTP 404"* ]]; then
    echo "Could not check the state release; backup upload stopped." >&2
    exit 1
  fi
  gh release create "${state_tag}" \
    --repo "${tracker_repo}" \
    --title "Tracker state" \
    --notes "Encrypted SQLite state and rotating backups. The decryption key is stored in Actions secrets." \
    --prerelease
fi

gh release upload "${state_tag}" \
  "${backup_dir}/${stable_name}" \
  "${backup_dir}/${stable_name}.sha256" \
  "${backup_dir}/${daily_name}" \
  "${backup_dir}/${daily_name}.sha256" \
  --repo "${tracker_repo}" \
  --clobber

oldest_keep="$(date -u -d "$((retention_days - 1)) days ago" +%F)"
release_id="$(
  gh api "repos/${tracker_repo}/releases/tags/${state_tag}" --jq '.id'
)"
while IFS=$'\t' read -r asset_id asset_name; do
  if [[ "${asset_name}" =~ ^ot-tracker-([0-9]{4}-[0-9]{2}-[0-9]{2})\.sqlite3\.gz\.gpg(\.sha256)?$ ]]; then
    asset_date="${BASH_REMATCH[1]}"
    if [[ "${asset_date}" < "${oldest_keep}" ]]; then
      gh api --method DELETE \
        "repos/${tracker_repo}/releases/assets/${asset_id}" >/dev/null
      echo "Pruned expired tracker backup ${asset_name}."
    fi
  fi
done < <(
  gh api --paginate \
    "repos/${tracker_repo}/releases/${release_id}/assets?per_page=100" \
    --jq '.[] | "\(.id)\t\(.name)"'
)

echo "Persisted stable state and daily backup ${daily_name}."
