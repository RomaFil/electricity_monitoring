#!/bin/bash
# Щоденний бекап бази Svitlo-Local (cron 04:30).
#
# Ті самі два принципи, що в ~/air-alerts/alerts-backup.sh:
#   1. Знімок через VACUUM INTO, а не копія файлу: база в режимі WAL і пишеться
#      щохвилини, проста копія може зловити її посеред транзакції.
#   2. Старі копії видаляються ТІЛЬКИ після того, як свіжий архів перевірено.
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

APP_DIR="${SVITLO_APP_DIR:-$HOME/svitlo-local}"
DEST="${SVITLO_BACKUP_DIR:-$HOME/backups/svitlo-local}"
KEEP="${SVITLO_KEEP:-14}"   # скільки останніх архівів лишати
LOG="$APP_DIR/backup.log"

mkdir -p "$DEST"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }
die() { say "ПОМИЛКА: $*"; echo "ПОМИЛКА: $*" >&2; exit 1; }

cd "$APP_DIR"

TMP="$(mktemp "$DEST/.tmp.XXXXXX")"
SNAP="/data/.backup-snapshot.db"
cleanup() {
    rm -f "$TMP"
    docker compose exec -T web sh -c "rm -f $SNAP" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Знімок + перевірка цілісності всередині контейнера. Назовні йде лише підсумок.
summary=$(docker compose exec -T web python - "$SNAP" <<'PY'
import os, sqlite3, sys
snap = sys.argv[1]
if os.path.exists(snap):
    os.remove(snap)
src = sqlite3.connect("/data/svitlo.db", timeout=120)
src.execute("PRAGMA busy_timeout=120000")
src.execute("VACUUM INTO ?", (snap,))
src.close()
chk = sqlite3.connect(snap)
if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    sys.exit("integrity_check не пройдено")
counts = {t: chk.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("events", "pings", "power_samples")}
chk.close()
print(" ".join(f"{k}={v}" for k, v in counts.items()))
PY
) || die "знімок не пройшов перевірку: $summary"

summary="$(echo "$summary" | tr -d '\r')"

docker compose exec -T web sh -c "cat $SNAP" > "$TMP" || die "не вдалось вивантажити знімок"
[ -s "$TMP" ] || die "вивантажений знімок порожній"

OUT="$DEST/svitlo-$(date +%F).db.gz"
gzip -c "$TMP" > "$OUT.part" && mv "$OUT.part" "$OUT"
chmod 600 "$OUT"
say "ok: $OUT, $summary, $(stat -c%s "$OUT") байт"

# Ротація — лише тепер, коли свіжий архів перевірений. Лишаємо $KEEP найновіших.
ls -1t "$DEST"/svitlo-*.db.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
say "ротація: лишилось $(ls -1 "$DEST"/svitlo-*.db.gz | wc -l) архівів"
