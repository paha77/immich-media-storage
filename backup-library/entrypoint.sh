#!/bin/sh
set -e

echo "${BACKUP_SCHEDULE} rsync -a --delete /source/ /destination/" | crontab -
echo "Library backup scheduled: ${BACKUP_SCHEDULE}"
crond -f -l 6
