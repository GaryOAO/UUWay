#!/usr/bin/env bash
# Root-owned copy is run by a bounded system timer, independent of GNOME:
#   mutter-trial-rollback.sh DROPIN_SHA256 DESKTOP_USER
# Restores only this trial's exact drop-in; leaves system libraries untouched.
set -Eeuo pipefail
[[ "$EUID" == 0 && "$#" == 2 && "$1" =~ ^[0-9a-f]{64}$ && "$2" =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 2
user="$2"
uid="$(id -u -- "$user")"
home="$(getent passwd -- "$user" | cut -d: -f6)"
trial=/run/uurb-mutter-trial
config="$home/.config/systemd/user/org.gnome.Shell@wayland.service.d/92-uurb-mutter-jitter.conf"
backup="$config.rolled-back"
if [[ -f "$trial/confirmed" && ! -L "$trial/confirmed" && "$(stat -c %u "$trial/confirmed")" == 0 ]]; then
    printf 'Mutter trial confirmed; automatic rollback not needed.\n'
    exit 0
fi
[[ -f "$config" && ! -L "$config" && ! -e "$backup" && ! -L "$backup" ]] || exit 3
[[ "$(sha256sum "$config" | cut -d' ' -f1)" == "$1" ]] || exit 4
mv --no-clobber -- "$config" "$backup"
[[ ! -e "$config" ]] || exit 5
runuser -u "$user" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user daemon-reload
printf 'Unconfirmed Mutter trial rolled back; restarting GDM on original libraries.\n'
systemctl restart gdm.service
