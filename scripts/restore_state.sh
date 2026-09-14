#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${STATE_ENCRYPTION_KEY:?STATE_ENCRYPTION_KEY is required for encrypted backups}"

tracker_repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
state_tag="${STATE_RELEASE_TAG:-tracker-state}"
state_db="${STATE_DB:-var/ot-tracker.sqlite3}"
runner_tmp="${RUNNER_TEMP:-/tmp}"
restore_dir="$(mktemp -d "${runner_tmp}/ot-tracker-restore.XXXXXX")"
trap 'rm -rf -- "${restore_dir}"' EXIT

if ! release_lookup="$(gh api "repos/${tracker_repo}/releases/tags/${state_tag}" --jq '.id' 2>&1)"; then
  if [[ "${release_lookup}" == *"HTTP 404"* ]]; then
    echo "No tracker-state release exists; starting with a fresh database."
    exit 0
  fi
  echo "Could not read the state release; refusing to start with an empty database." >&2
  exit 1
fi
install -d -m 700 "${restore_dir}/gnupg"

mapfile -t release_assets < <(
  gh release view "${state_tag}" \
    --repo "${tracker_repo}" \
    --json assets \
    --jq '.assets[].name'
)

asset_exists() {
  local expected="$1"
  local asset
  for asset in "${release_assets[@]}"; do
    if [[ "${asset}" == "${expected}" ]]; then
      return 0
    fi
  done
  return 1
}

candidates=()
if asset_exists "ot-tracker.sqlite3.gz.gpg"; then
  candidates+=("ot-tracker.sqlite3.gz.gpg")
fi
while IFS= read -r asset; do
  [[ -n "${asset}" ]] && candidates+=("${asset}")
done < <(
  printf '%s\n' "${release_assets[@]}" \
    | awk '/^ot-tracker-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sqlite3\.gz\.gpg$/' \
    | sort -r
)

if ((${#candidates[@]} == 0)); then
  echo "Tracker-state release contains no usable SQLite archives."
  exit 1
fi

mkdir -p "$(dirname "${state_db}")"
for asset in "${candidates[@]}"; do
  archive="${restore_dir}/${asset}"
  checksum_name="${asset}.sha256"
  candidate_db="${restore_dir}/candidate.sqlite3"
  compressed_db="${restore_dir}/candidate.sqlite3.gz"

  if ! gh release download "${state_tag}" \
    --repo "${tracker_repo}" \
    --pattern "${asset}" \
    --dir "${restore_dir}" \
    --clobber; then
    echo "Could not download ${asset}; trying the next backup."
    continue
  fi

  if asset_exists "${checksum_name}"; then
    if ! gh release download "${state_tag}" \
      --repo "${tracker_repo}" \
      --pattern "${checksum_name}" \
      --dir "${restore_dir}" \
      --clobber; then
      echo "Could not download ${checksum_name}; trying the next backup."
      continue
    fi
    if ! (
      cd "${restore_dir}"
      sha256sum --check "${checksum_name}" >/dev/null
    ); then
      echo "Checksum validation failed for ${asset}; trying the next backup."
      continue
    fi
  else
    echo "No checksum for ${asset}; trying the next backup."
    continue
  fi

  if ! gpg \
    --homedir "${restore_dir}/gnupg" \
    --batch --yes --no-tty --pinentry-mode loopback --no-symkey-cache \
    --passphrase-fd 3 --output "${compressed_db}" --decrypt "${archive}" \
    3<<<"${STATE_ENCRYPTION_KEY}"; then
    echo "Decryption failed for ${asset}; trying the next backup."
    continue
  fi
  if ! gzip --test "${compressed_db}"; then
    echo "gzip validation failed for ${asset}; trying the next backup."
    continue
  fi
  if ! gzip --decompress --stdout "${compressed_db}" >"${candidate_db}"; then
    echo "Could not decompress ${asset}; trying the next backup."
    continue
  fi
  if ! python3 - "${candidate_db}" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    result = connection.execute("PRAGMA quick_check").fetchone()
finally:
    connection.close()
raise SystemExit(0 if result and result[0] == "ok" else 1)
PY
  then
    echo "SQLite quick_check failed for ${asset}; trying the next backup."
    continue
  fi

  install -m 600 "${candidate_db}" "${state_db}"
  echo "Restored tracker state from ${asset}."
  exit 0
done

echo "Every tracker-state backup failed validation."
exit 1
