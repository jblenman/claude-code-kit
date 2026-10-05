#!/bin/bash
# install-collector.sh - install or update the OpenTelemetry Collector (otelcol-contrib) on a
# Debian or Ubuntu host (arm64 or amd64; the CPU is detected) as the receiver for Claude Code
# telemetry.
#
# Native binary under systemd, no container runtime. Idempotent: every step checks the current
# state first, and a re-run with nothing to do changes nothing.
#
#   sudo ./install-collector.sh                       # install/update collector + config + unit
#   sudo ./install-collector.sh --with-prometheus     # also apt-install Prometheus and add the scrape job
#   ./install-collector.sh --dry-run                  # print what would change, write nothing (no root needed)
#   sudo OTELCOL_VERSION=0.162.0 ./install-collector.sh   # pin a release instead of resolving "latest"
#   sudo PROM_RETENTION=90d ./install-collector.sh --with-prometheus   # Prometheus retention (default 2y)
#   sudo ./install-collector.sh --config /path/to/otelcol-config.yaml
#
# Run it on the collector host from a clone of this repo (cd telemetry), or from another machine:
#   ssh <collector-host> 'sudo -n bash -s -- --dry-run' < install-collector.sh
# (then the config must be given with --config or already be at /etc/otelcol-contrib/config.yaml).
#
# What it manages:
#   /usr/local/bin/otelcol-contrib                 the release binary for this CPU (arm64 or amd64)
#   /etc/otelcol-contrib/config.yaml               from otelcol-config.yaml next to this script
#   /etc/systemd/system/otelcol-contrib.service    the unit (enabled, started, restarted on change)
#   /var/lib/otelcol-contrib/events/               the events JSONL files (owner otelcol-contrib)
#   system user otelcol-contrib                    no shell, no home
#   --with-prometheus: apt package `prometheus`, scrape job in /etc/prometheus/prometheus.yml,
#   retention set in /etc/default/prometheus
#
# Network use: two or three requests to github.com (release redirect, tarball, checksums file),
# plus apt for --with-prometheus.
set -euo pipefail
umask 022

REPO=open-telemetry/opentelemetry-collector-releases
BIN=/usr/local/bin/otelcol-contrib
CONF_DIR=/etc/otelcol-contrib
CONF=$CONF_DIR/config.yaml
UNIT=/etc/systemd/system/otelcol-contrib.service
DATA_DIR=/var/lib/otelcol-contrib
SVC_USER=otelcol-contrib
PROM_YML=/etc/prometheus/prometheus.yml
PROM_DEFAULT=/etc/default/prometheus
PROM_RETENTION=${PROM_RETENTION:-2y}

HERE=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)
SRC_CONF="$HERE/otelcol-config.yaml"
SRC_SCRAPE="$HERE/prometheus-scrape.yaml"
OS_RELEASE=${OS_RELEASE_FILE:-/etc/os-release}
DRY=0
WITH_PROM=0
changed=0
need_restart=0

usage() { sed -n '2,30p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --with-prometheus) WITH_PROM=1 ;;
        --config) shift; SRC_CONF=${1:-}; [ -n "$SRC_CONF" ] || { echo "--config needs a path" >&2; exit 2; } ;;
        --config=*) SRC_CONF=${1#--config=} ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
    shift
done

log()  { printf '%s\n' "$*"; }
act()  { changed=1; if [ "$DRY" = 1 ]; then log "WOULD  $*"; else log "DO     $*"; fi; }
keep() { log "OK     $*"; }
warn() { log "WARN   $*"; }
die()  { echo "ERROR  $*" >&2; exit 1; }
run()  { [ "$DRY" = 1 ] || "$@"; }

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "run as root (sudo), or use --dry-run"
fi

# --- operating system ------------------------------------------------------------------------
# Debian family only: the Prometheus step uses apt and /etc/default/prometheus, the unit needs systemd.
os_id=""; os_like=""; os_name="unknown"
if [ -r "$OS_RELEASE" ]; then
    os_id=$(. "$OS_RELEASE" 2>/dev/null; printf '%s' "${ID:-}")
    os_like=$(. "$OS_RELEASE" 2>/dev/null; printf '%s' "${ID_LIKE:-}")
    os_name=$(. "$OS_RELEASE" 2>/dev/null; printf '%s' "${PRETTY_NAME:-${ID:-unknown}}")
fi
case " $os_id $os_like " in
    *" debian "*|*" ubuntu "*) log "os     $os_name" ;;
    *)
        if [ "$DRY" = 1 ]; then
            warn "not a Debian or Ubuntu host ($os_name); a real run would stop here"
        else
            die "this script supports Debian and Ubuntu (and derivatives such as Raspberry Pi OS); found: $os_name. On another distribution follow the steps in README.md by hand."
        fi
        ;;
esac
if [ "$DRY" = 0 ]; then
    for c in curl tar sha256sum install systemctl useradd; do
        command -v "$c" >/dev/null 2>&1 || die "missing command: $c"
    done
fi

# --- CPU architecture -> release asset name --------------------------------------------------
case "$(uname -m)" in
    aarch64|arm64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=amd64 ;;
    armv7l|armv6l) die "32-bit ARM OS detected; install the 64-bit OS (this script handles arm64 and amd64)" ;;
    *)             die "unsupported CPU architecture: $(uname -m) (this script handles arm64 and amd64)" ;;
esac
log "arch   $ARCH"

# --- release version -------------------------------------------------------------------------
# OTELCOL_VERSION pins a release (plain "0.162.0", no v). Otherwise follow GitHub's "latest" redirect.
VERSION=${OTELCOL_VERSION:-}
if [ -z "$VERSION" ]; then
    loc=$(curl -fsSI --max-time 30 "https://github.com/$REPO/releases/latest" 2>/dev/null \
          | tr -d '\r' | awk 'tolower($1)=="location:" {print $2}' | tail -n 1) || true
    VERSION=${loc##*/tag/v}
    [ -n "$VERSION" ] && [ "$VERSION" != "$loc" ] || die "could not resolve the latest release from github.com; set OTELCOL_VERSION=x.y.z"
fi
case "$VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) die "OTELCOL_VERSION must look like 0.162.0 (got '$VERSION')" ;;
esac
ASSET="otelcol-contrib_${VERSION}_linux_${ARCH}.tar.gz"
BASE="https://github.com/$REPO/releases/download/v$VERSION"
log "target otelcol-contrib $VERSION ($ASSET)"

# --- binary ----------------------------------------------------------------------------------
installed=""
if [ -x "$BIN" ]; then
    installed=$("$BIN" --version 2>/dev/null | sed -n 's/.*version v\{0,1\}\([0-9][0-9.]*\).*/\1/p' | head -n 1)
fi
if [ "$installed" = "$VERSION" ]; then
    keep "$BIN is already $VERSION"
else
    act "install $BIN $VERSION (installed: ${installed:-none})"
    need_restart=1
    if [ "$DRY" = 0 ]; then
        tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
        curl -fsSL --max-time 600 -o "$tmp/$ASSET" "$BASE/$ASSET" || die "download failed: $BASE/$ASSET"
        # Checksums file published with every release; verify when it is there and names our asset.
        if curl -fsSL --max-time 60 -o "$tmp/sums.txt" "$BASE/opentelemetry-collector-releases_otelcol-contrib_checksums.txt" 2>/dev/null \
           && grep -q " $ASSET\$" "$tmp/sums.txt"; then
            (cd "$tmp" && grep " $ASSET\$" sums.txt | sha256sum -c --quiet -) || die "sha256 mismatch for $ASSET"
            log "       sha256 verified against the release checksums file"
        else
            warn "checksums file not found for this release; sha256 not verified"
        fi
        tar -xzf "$tmp/$ASSET" -C "$tmp" otelcol-contrib || die "tarball does not contain otelcol-contrib"
        install -m 0755 "$tmp/otelcol-contrib" "$BIN.new"
        "$BIN.new" --version >/dev/null || die "the downloaded binary does not run"
        mv -f "$BIN.new" "$BIN"
    fi
fi

# --- service user and directories ------------------------------------------------------------
if id -u "$SVC_USER" >/dev/null 2>&1; then
    keep "user $SVC_USER exists"
else
    act "create system user $SVC_USER"
    run useradd --system --no-create-home --shell /usr/sbin/nologin --user-group "$SVC_USER"
fi
for d in "$CONF_DIR" "$DATA_DIR" "$DATA_DIR/events"; do
    if [ -d "$d" ]; then keep "dir $d"; else act "mkdir $d"; run mkdir -p "$d"; fi
done
if [ "$DRY" = 0 ]; then
    chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR"
    chmod 0750 "$DATA_DIR" "$DATA_DIR/events"
fi

# --- config ----------------------------------------------------------------------------------
if [ ! -f "$SRC_CONF" ]; then
    if [ -f "$CONF" ]; then
        keep "no source config next to the script; keeping the installed $CONF"
    else
        die "no config: put otelcol-config.yaml next to this script or pass --config PATH"
    fi
elif [ -f "$CONF" ] && cmp -s "$SRC_CONF" "$CONF"; then
    keep "$CONF matches $SRC_CONF"
else
    act "install $CONF from $SRC_CONF"
    need_restart=1
    if [ "$DRY" = 0 ]; then
        install -m 0644 "$SRC_CONF" "$CONF.new"
        if [ -x "$BIN" ]; then
            "$BIN" validate --config="$CONF.new" || { rm -f "$CONF.new"; die "the new config does not validate; nothing changed"; }
        fi
        mv -f "$CONF.new" "$CONF"
    fi
fi

# --- systemd unit ----------------------------------------------------------------------------
unit_text=$(cat <<EOF
[Unit]
Description=OpenTelemetry Collector (contrib) - Claude Code telemetry
Documentation=https://opentelemetry.io/docs/collector/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
ExecStart=$BIN --config=$CONF
Restart=on-failure
RestartSec=5s
# Hardening: the collector only needs to listen on 4317/4318/8889 and write under $DATA_DIR.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
ReadWritePaths=$DATA_DIR
MemoryMax=400M

[Install]
WantedBy=multi-user.target
EOF
)
if [ -f "$UNIT" ] && [ "$(cat "$UNIT")" = "$unit_text" ]; then
    keep "$UNIT"
else
    act "write $UNIT"
    need_restart=1
    if [ "$DRY" = 0 ]; then
        printf '%s\n' "$unit_text" > "$UNIT"
        systemctl daemon-reload
    fi
fi

# --- enable / start / restart ----------------------------------------------------------------
if [ "$DRY" = 1 ]; then
    [ "$need_restart" = 1 ] && act "enable + (re)start otelcol-contrib.service" || keep "service unchanged"
else
    if ! systemctl is-enabled --quiet otelcol-contrib.service 2>/dev/null; then
        act "systemctl enable otelcol-contrib.service"; systemctl enable --quiet otelcol-contrib.service
    fi
    if [ "$need_restart" = 1 ] || ! systemctl is-active --quiet otelcol-contrib.service; then
        act "systemctl restart otelcol-contrib.service"
        systemctl restart otelcol-contrib.service
        sleep 2
        systemctl is-active --quiet otelcol-contrib.service || { journalctl -u otelcol-contrib -n 30 --no-pager; die "service failed to start"; }
    else
        keep "otelcol-contrib.service active"
    fi
fi

# --- optional: Prometheus server -------------------------------------------------------------
if [ "$WITH_PROM" = 1 ]; then
    case "$PROM_RETENTION" in
        [0-9]*[smhdwy]) ;;
        *) die "PROM_RETENTION must look like 90d or 2y (got '$PROM_RETENTION')" ;;
    esac
    if dpkg -s prometheus >/dev/null 2>&1; then
        keep "apt package prometheus installed"
    else
        act "apt-get install prometheus"
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q prometheus
    fi
    if [ -f "$PROM_YML" ] && grep -q 'job_name: otelcol-claude' "$PROM_YML"; then
        keep "scrape job otelcol-claude present in $PROM_YML"
    elif [ -f "$SRC_SCRAPE" ]; then
        act "append scrape job otelcol-claude to $PROM_YML"
        if [ "$DRY" = 0 ]; then
            [ -f "$PROM_YML" ] || die "$PROM_YML missing after install"
            grep -q '^scrape_configs:' "$PROM_YML" || die "$PROM_YML has no scrape_configs: section; add the job from $SRC_SCRAPE by hand"
            # The Debian/Ubuntu default file ends with the scrape_configs list, so appending extends that list.
            cp -a "$PROM_YML" "$PROM_YML.bak-$(date +%Y%m%d%H%M%S)"
            { printf '\n  # claude-code-kit telemetry - Claude Code metrics via otelcol-contrib\n'; grep -v '^#' "$SRC_SCRAPE"; } >> "$PROM_YML"
            if command -v promtool >/dev/null && ! promtool check config "$PROM_YML" >/dev/null; then
                mv -f "$(ls -t "$PROM_YML".bak-* | head -n 1)" "$PROM_YML"
                die "promtool rejected the appended scrape job; prometheus.yml restored"
            fi
            systemctl restart prometheus
        fi
    else
        warn "$SRC_SCRAPE not found; add the scrape job by hand (see README.md)"
    fi
    if [ -f "$PROM_DEFAULT" ] && grep -q 'storage.tsdb.retention.time' "$PROM_DEFAULT"; then
        keep "retention already set in $PROM_DEFAULT"
    elif [ -f "$PROM_DEFAULT" ] || [ "$DRY" = 1 ]; then
        act "set --storage.tsdb.retention.time=$PROM_RETENTION in $PROM_DEFAULT (the package default is 15 days)"
        if [ "$DRY" = 0 ]; then
            if grep -q '^ARGS=' "$PROM_DEFAULT"; then
                sed -i "s|^ARGS=\"\(.*\)\"|ARGS=\"\1 --storage.tsdb.retention.time=$PROM_RETENTION\"|" "$PROM_DEFAULT"
            else
                printf 'ARGS="--storage.tsdb.retention.time=%s"\n' "$PROM_RETENTION" >> "$PROM_DEFAULT"
            fi
            systemctl restart prometheus
        fi
    fi
fi

# --- where to point the machines -------------------------------------------------------------
addr=$(hostname -I 2>/dev/null | awk '{print $1}') || true
[ -n "${addr:-}" ] || addr=$(hostname 2>/dev/null || echo "<collector-host>")
log ""
if [ "$DRY" = 1 ]; then
    log "DRY RUN finished: $( [ "$changed" = 1 ] && echo 'changes listed above' || echo 'nothing to change')"
else
    log "done: $( [ "$changed" = 1 ] && echo 'changes applied' || echo 'nothing to change')"
    log "check: systemctl status otelcol-contrib --no-pager; curl -s http://127.0.0.1:8889/metrics | head"
fi
log "machines export to: http://$addr:4317 (gRPC) or http://$addr:4318 (HTTP); COLLECTOR_HOST=$addr in settings-env.json"
