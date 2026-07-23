#!/usr/bin/env bash
# Render start script. If a persistent disk is mounted at /var/data, move all
# mutable state (client_data, batches, posted) onto it via symlinks so batches,
# rules, and audit trails survive deploys. Without a disk, run as-is
# (free tier: state resets on each deploy — fine for trials).
set -e

if [ -d /var/data ]; then
  if [ ! -f /var/data/.seeded ]; then
    cp -r client_data /var/data/client_data
    mkdir -p /var/data/batches /var/data/posted
    touch /var/data/.seeded
  else
    # Sync master/reference data from the repo on every deploy WITHOUT
    # touching learned state (rules.json, audit.jsonl, posted_registry.json,
    # file_log.json stay) and WITHOUT resurrecting clients that were deleted
    # in the console — only clients still present on the disk are updated.
    for dir in client_data/*/; do
      name=$(basename "$dir")
      [ -d "/var/data/client_data/$name" ] || continue
      for f in chart_of_accounts.csv suppliers.csv customers.csv tax_codes.csv; do
        [ -f "$dir$f" ] && cp -f "$dir$f" "/var/data/client_data/$name/$f"
      done
    done
    for f in client_data/*.yaml; do
      [ -f "$f" ] && cp -f "$f" "/var/data/client_data/$(basename "$f")"
    done
  fi
  rm -rf client_data batches posted
  ln -s /var/data/client_data client_data
  ln -s /var/data/batches batches
  ln -s /var/data/posted posted
fi

exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8600}"
