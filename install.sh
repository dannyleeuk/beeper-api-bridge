#!/usr/bin/env bash
# beeper-api-bridge installer / updater for Debian and Ubuntu.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/dannyleeuk/beeper-api-bridge/main/install.sh)
#
# Installs python3 + PyYAML and, if missing, bbctl (Beeper's bridge manager); puts the bridge under /opt/beeper-api-bridge;
# registers the appservice with Beeper; writes config.json; installs two systemd units (bridge + `bbctl proxy`) and sends
# a test notification. Re-running offers Update (new code, config untouched), Add application, Show tokens, or Reinstall.
#
# Already running bbctl as your own user (e.g. `bbctl run sh-whatsapp` in tmux)? The installer finds that bbctl and its
# login and runs the bridge as the same user, so there is no second login. With --no-systemd it skips the units and
# prints the two commands to run yourself (tmux, screen, your own supervisor).
#
# Options:  --dir DIR        install directory (default /opt/beeper-api-bridge)
#           --user NAME      run as this user (default: the user who ran sudo if they have a bbctl login, else `bridge`)
#           --ref REF        git ref to install (default: latest release tag; use "main" for the development branch)
#           --no-systemd     do not install/start systemd units; print the commands to run instead
#           --update         non-interactive update of an existing install
#           --yes            accept defaults for every prompt (still stops for `bbctl login` if needed)
set -euo pipefail

REPO=https://github.com/dannyleeuk/beeper-api-bridge.git
API=https://api.github.com/repos/dannyleeuk/beeper-api-bridge
BBCTL_API=https://api.github.com/repos/beeper/bridge-manager/releases/latest
DIR=/opt/beeper-api-bridge
SVC_USER=""
REF=""
MODE=""
YES=0
SYSTEMD=1
API_PORT=29338
AS_PORT=29337

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR=$2; shift 2 ;;
    --user) SVC_USER=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --update) MODE=update; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_0=$'\e[0m'
say()  { printf '%s==>%s %s\n' "$c_ok" "$c_0" "$*"; }
warn() { printf '%s!!%s %s\n' "$c_warn" "$c_0" "$*" >&2; }
die()  { printf '%sERROR:%s %s\n' "$c_err" "$c_0" "$*" >&2; exit 1; }
ask()  { # ask VAR "prompt" "default"
  local var=$1 prompt=$2 def=${3:-} _r=""
  if [ "$YES" = 1 ]; then printf -v "$var" '%s' "$def"; return; fi
  { read -r -p "$prompt${def:+ [$def]}: " _r </dev/tty; } 2>/dev/null || read -r _r || true
  printf -v "$var" '%s' "${_r:-$def}"
}
as_user() { sudo -u "$SVC_USER" env HOME="$SVC_HOME" PATH="$PATH" "$@"; }
mint()    { python3 -c "import secrets,string,sys; print(sys.argv[1]+''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(29)))" "$1"; }   # 30 chars, letters+digits, like the format some senders validate

# ---- preflight --------------------------------------------------------------------------------------------------
[ "$(id -u)" = 0 ] || die "run as root (sudo bash install.sh)"
[ -r /etc/os-release ] && . /etc/os-release
case "${ID:-}${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) die "this installer supports Debian and Ubuntu (found ${PRETTY_NAME:-unknown})" ;; esac
case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) die "unsupported CPU architecture $(uname -m)" ;; esac

say "Installing prerequisites (python3, PyYAML, curl, git)"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq python3 python3-yaml curl git ca-certificates >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq python3 python3-yaml curl git ca-certificates >/dev/null; }

# ---- which user, which bbctl ------------------------------------------------------------------------------------
# An existing bbctl login lives in ~/.config/bbctl of whoever ran `bbctl login`. If the person running this installer
# (via sudo) already has one, run the bridge as them and reuse it; otherwise use a dedicated system user.
find_bbctl() { # find_bbctl HOME -> path or empty
  local h=$1 p
  for p in "$(command -v bbctl 2>/dev/null || true)" /usr/local/bin/bbctl "$h"/bbctl "$h"/bbctl/bbctl "$h"/bbctl/bbctl-linux-$ARCH "$h"/.local/bin/bbctl "$h"/bin/bbctl; do
    [ -n "$p" ] && [ -x "$p" ] && { echo "$p"; return; }
  done
}
CALLER=${SUDO_USER:-}
if [ -n "$CALLER" ] && [ "$CALLER" != root ]; then CALLER_HOME=$(getent passwd "$CALLER" | cut -d: -f6); else CALLER_HOME=""; fi
if [ -z "$SVC_USER" ]; then
  if [ -n "$CALLER" ] && [ -d "$CALLER_HOME/.config/bbctl" ]; then
    echo "Found a bbctl login for user '$CALLER' ($CALLER_HOME/.config/bbctl)."
    ask reply "Run the bridge as '$CALLER' and reuse that login? (y/n)" y
    case "$reply" in y|Y|yes) SVC_USER=$CALLER ;; *) SVC_USER=bridge ;; esac
  else
    SVC_USER=bridge
  fi
fi
if ! id "$SVC_USER" >/dev/null 2>&1; then
  say "Creating service user '$SVC_USER'"
  useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
SVC_HOME=$(getent passwd "$SVC_USER" | cut -d: -f6); { [ -n "$SVC_HOME" ] && [ "$SVC_HOME" != / ]; } || SVC_HOME=$DIR
mkdir -p "$DIR"; chown "$SVC_USER:$SVC_USER" "$DIR"; chmod 750 "$DIR"
say "Bridge runs as '$SVC_USER' (home $SVC_HOME), files in $DIR"

BBCTL=$(find_bbctl "$SVC_HOME")
if [ -z "$BBCTL" ]; then
  say "Installing bbctl (Beeper bridge manager) to /usr/local/bin"
  url=$(curl -fsSL "$BBCTL_API" | python3 -c "import json,sys; a=[x['browser_download_url'] for x in json.load(sys.stdin)['assets'] if x['name']=='bbctl-linux-$ARCH']; print(a[0] if a else '')")
  [ -n "$url" ] || die "could not find a bbctl release for linux-$ARCH"
  curl -fsSL "$url" -o /usr/local/bin/bbctl.tmp && chmod 755 /usr/local/bin/bbctl.tmp && mv /usr/local/bin/bbctl.tmp /usr/local/bin/bbctl
  BBCTL=/usr/local/bin/bbctl
fi
say "bbctl: $BBCTL ($("$BBCTL" --version 2>/dev/null | head -1))"

# ---- code -------------------------------------------------------------------------------------------------------
SRC=$DIR/src
if [ -z "$REF" ]; then
  REF=$(curl -fsSL "$API/releases/latest" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin).get('tag_name',''))" 2>/dev/null || true)
  [ -n "$REF" ] || REF=main
fi
if [ -d "$SRC/.git" ]; then
  say "Fetching $REF"
  as_user git -C "$SRC" fetch -q --tags origin
else
  say "Cloning $REPO ($REF)"
  as_user git clone -q "$REPO" "$SRC"
fi
as_user git -C "$SRC" checkout -q "$REF" 2>/dev/null || as_user git -C "$SRC" checkout -q "origin/$REF"
[ "$REF" = main ] && as_user git -C "$SRC" pull -q --ff-only origin main || true
install -o "$SVC_USER" -g "$SVC_USER" -m 755 "$SRC/beeper_api_bridge.py" "$DIR/beeper_api_bridge.py"
VERSION=$(python3 "$DIR/beeper_api_bridge.py" --version)
say "Installed $VERSION"

# ---- systemd units (paths match $DIR) ----------------------------------------------------------------------------
install_units() {
  for u in beeper-api-bridge.service beeper-api-bridge-proxy.service; do
    sed -e "s|/opt/beeper-api-bridge|$DIR|g" -e "s|^User=.*|User=$SVC_USER|" -e "s|^Group=.*|Group=$(id -gn "$SVC_USER")|" \
        -e "s|^Environment=HOME=.*|Environment=HOME=$SVC_HOME|" -e "s|/usr/local/bin/bbctl|$BBCTL|" -e "s|127.0.0.1:29337|127.0.0.1:$AS_PORT|" \
        -e "s|^ProtectSystem=full|ProtectSystem=full\nReadWritePaths=$DIR $SVC_HOME/.config|" "$SRC/contrib/$u" > "/etc/systemd/system/$u"
  done
  systemctl daemon-reload
  systemctl enable -q beeper-api-bridge.service beeper-api-bridge-proxy.service
}
manual_commands() {
  echo
  echo "Run these two (e.g. in a tmux session each; the proxy needs the bridge up first):"
  echo "  sudo -u $SVC_USER HOME=$SVC_HOME python3 $DIR/beeper_api_bridge.py --config $DIR/config.json"
  echo "  sudo -u $SVC_USER HOME=$SVC_HOME $BBCTL proxy -r $DIR/registration.yaml"
  echo "(as '$SVC_USER' yourself, drop the sudo prefix)"
}
restart_all() {
  [ "$SYSTEMD" = 1 ] || { manual_commands; return; }
  systemctl restart beeper-api-bridge.service
  for i in $(seq 1 30); do curl -fs "http://127.0.0.1:$API_PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
  systemctl restart beeper-api-bridge-proxy.service
}
show_tokens() {
  python3 - "$DIR/config.json" "$API_PORT" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); port = sys.argv[2]
print(f"\nSend notifications to:  http://<this host>:{port}/1/messages.json   (bound to {c.get('api_bind','127.0.0.1')})")
print(f"user key (the `user` parameter): {c['user_key']}\n")
print(f"{'application':24} token")
for t, a in c["applications"].items():
    print(f"{a.get('name',''):24} {t}")
print("\nExample:\n  curl -sS -X POST http://127.0.0.1:%s/1/messages.json --data-urlencode token=<token> --data-urlencode user=%s \\\n       --data-urlencode title=Hello --data-urlencode 'message=first notification'" % (port, c['user_key']))
PY
}
send_test() {
  local tok user
  tok=$(python3 -c "import json; print(next(iter(json.load(open('$DIR/config.json'))['applications'])))")
  user=$(python3 -c "import json; print(json.load(open('$DIR/config.json'))['user_key'])")
  curl -sS -X POST "http://127.0.0.1:$API_PORT/1/messages.json" --data-urlencode "token=$tok" --data-urlencode "user=$user" \
       --data-urlencode "title=beeper-api-bridge" --data-urlencode "message=Installed and working. This chat was created by the first application in config.json." >/dev/null \
    && say "Test notification sent — check Beeper for a new chat (accept the message request if asked)" \
    || warn "test notification failed: journalctl -u beeper-api-bridge -n 30"
}

# ---- existing install: menu --------------------------------------------------------------------------------------
if [ -s "$DIR/config.json" ] && [ -z "$MODE" ]; then
  if [ "$YES" = 1 ]; then MODE=update; else
    echo; echo "Existing installation found in $DIR."
    echo "  1) Update        — new code, restart, config untouched"
    echo "  2) Add application — mint a token for another sender"
    echo "  3) Show tokens   — print the user key and application tokens"
    echo "  4) Reinstall     — keep registration, rewrite config (tokens change!)"
    echo "  5) Quit"
    ask choice "Choice" 1
    case "$choice" in 1) MODE=update ;; 2) MODE=addapp ;; 3) show_tokens; exit 0 ;; 4) MODE=install ;; *) exit 0 ;; esac
  fi
fi
[ -n "$MODE" ] || MODE=install

case "$MODE" in
  update)
    [ -s "$DIR/config.json" ] || die "nothing to update: no $DIR/config.json (run without --update to install)"
    if [ "$SYSTEMD" = 1 ] && [ -f /etc/systemd/system/beeper-api-bridge.service ]; then install_units; restart_all
    else SYSTEMD=0; say "No systemd units installed here — restart your own bridge/proxy processes:"; manual_commands; fi
    say "Updated to $VERSION"
    exit 0 ;;
  addapp)
    ask name "Application name (as it will appear in Beeper)" ""
    [ -n "$name" ] || die "name required"
    tok=$(mint a)
    python3 - "$DIR/config.json" "$tok" "$name" <<'PY'
import json, sys, os
p, tok, name = sys.argv[1:]; c = json.load(open(p)); c["applications"][tok] = {"name": name}
tmp = p + ".tmp"; json.dump(c, open(tmp, "w"), indent=2); os.chmod(tmp, 0o600); os.replace(tmp, p)
PY
    chown "$SVC_USER:$SVC_USER" "$DIR/config.json"; restart_all
    say "Added '$name' with token $tok (a chat appears on its first message)"; exit 0 ;;
esac

# ---- fresh install ------------------------------------------------------------------------------------------------
say "Checking the Beeper login for user '$SVC_USER' (bbctl keeps it in $SVC_HOME/.config/bbctl)"
if ! as_user "$BBCTL" whoami >/dev/null 2>&1; then
  echo "Not logged in. bbctl will ask for your Beeper e-mail and the code it sends you."
  mkdir -p "$SVC_HOME/.config"; chown "$SVC_USER" "$SVC_HOME/.config"
  if { exec 3</dev/tty; } 2>/dev/null; then as_user "$BBCTL" login <&3 || die "bbctl login failed"; exec 3<&-
  else as_user "$BBCTL" login || die "bbctl login failed (needs an interactive terminal)"; fi
fi
ME=$(as_user "$BBCTL" whoami 2>/dev/null | awk -F': ' '/^User ID:/ {print $2}')
say "Beeper account: ${ME:-unknown}"

ask BRIDGE_NAME "Bridge name to register with Beeper (must start with sh-)" sh-apibridge
case "$BRIDGE_NAME" in sh-*) ;; *) die "Beeper requires self-hosted bridge names to start with sh-" ;; esac
ask OWNER "Your Matrix user ID (the chats are created for this user)" "${ME:-@you:beeper.com}"
ask APPS "Applications to create tokens for (comma-separated)" "Grafana, Uptime Kuma, Scripts"
ask API_BIND "Address for the notification API to listen on (127.0.0.1 = this host only)" 127.0.0.1

say "Registering '$BRIDGE_NAME' with Beeper"
REGJSON=$(as_user "$BBCTL" register --json "$BRIDGE_NAME") || die "bbctl register failed"
python3 - "$DIR" "$REGJSON" "$AS_PORT" <<'PY'
import json, os, sys, yaml
d, raw, port = sys.argv[1], sys.argv[2], sys.argv[3]
j = json.loads(raw); reg = j["registration"]
reg["url"] = f"http://127.0.0.1:{port}"          # bbctl proxy forwards appservice events here
p = os.path.join(d, "registration.yaml"); open(p, "w").write(yaml.safe_dump(reg, sort_keys=False)); os.chmod(p, 0o600)
meta = {"homeserver": j.get("homeserver_url", ""), "domain": j.get("homeserver_domain", ""), "user": j.get("your_user_id", "")}
open(os.path.join(d, ".register-meta.json"), "w").write(json.dumps(meta))
PY
HS=$(python3 -c "import json; print(json.load(open('$DIR/.register-meta.json'))['homeserver'])")
DOMAIN=$(python3 -c "import json; print(json.load(open('$DIR/.register-meta.json'))['domain'])")
[ -n "$HS" ] && [ -n "$DOMAIN" ] || die "bbctl did not return the homeserver URL/domain; see $DIR/.register-meta.json"
say "Homeserver $HS, domain $DOMAIN"

say "Writing $DIR/config.json"
USER_KEY=$(mint u)
python3 - "$DIR" "$HS" "$DOMAIN" "$OWNER" "$API_BIND" "$API_PORT" "$AS_PORT" "$USER_KEY" "$APPS" <<'PY'
import json, os, sys, secrets
d, hs, dom, owner, bind, aport, asport, ukey, apps = sys.argv[1:]
c = {"registration_file": os.path.join(d, "registration.yaml"), "homeserver": hs, "domain": dom, "owner": owner,
     "state_file": os.path.join(d, "state.json"),
     "appservice_bind": "127.0.0.1", "appservice_port": int(asport), "api_bind": bind, "api_port": int(aport),
     "user_key": ukey, "applications": {}, "single_room": ""}
import string
for name in [a.strip() for a in apps.split(",") if a.strip()]:
    c["applications"]["a" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(29))] = {"name": name}
p = os.path.join(d, "config.json"); open(p, "w").write(json.dumps(c, indent=2) + "\n"); os.chmod(p, 0o600)
PY
rm -f "$DIR/.register-meta.json"
chown "$SVC_USER:$SVC_USER" "$DIR/config.json" "$DIR/registration.yaml"

say "Validating config and homeserver login"
as_user python3 "$DIR/beeper_api_bridge.py" --check --config "$DIR/config.json" || die "check failed"

if [ "$SYSTEMD" = 1 ]; then
  say "Installing and starting systemd units"
  install_units; restart_all
  sleep 2; systemctl is-active -q beeper-api-bridge-proxy.service || warn "proxy not active yet: journalctl -u beeper-api-bridge-proxy -n 30"
  send_test
else
  manual_commands
  echo "Once both are running, send a test with the curl example below."
fi
show_tokens
echo
[ "$SYSTEMD" = 1 ] && say "Done. Manage with: systemctl status beeper-api-bridge beeper-api-bridge-proxy · journalctl -u beeper-api-bridge -f"
say "Re-run this script any time to update or add an application."
