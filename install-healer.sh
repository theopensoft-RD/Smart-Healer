SYSCTL=$(command -v systemctl)
PYBIN=/home/admin/.local/share/pipx/venvs/pat-smart/bin/python; [ -x "$PYBIN" ] || PYBIN=$(command -v python3)
echo "host=$(hostname) systemctl=$SYSCTL py=$PYBIN"
# --- temp files (admin · no sudo) ---
cat > /tmp/pat-healer.sudoers <<EOF
admin ALL=(root) NOPASSWD: $SYSCTL restart pat-smart-radar, $SYSCTL reset-failed pat-smart-radar, $SYSCTL restart pat-smart-stream, $SYSCTL reset-failed pat-smart-stream, $SYSCTL restart redis-server, $SYSCTL reset-failed redis-server
EOF
cat > /tmp/pfh.service <<EOF
[Unit]
Description=pat-fleet-healer node self-healing agent (ADR-037)
After=network-online.target
[Service]
Type=oneshot
User=admin
ExecStart=$PYBIN /home/admin/.config/pat-smart/workers/healer.py
EOF
cat > /tmp/pfh.timer <<EOF
[Unit]
Description=run pat-fleet-healer every 60s
[Timer]
OnBootSec=120
OnUnitActiveSec=60
[Install]
WantedBy=timers.target
EOF
# --- validate sudoers BEFORE install (broken sudoers = lockout) ---
if ! echo '__PW__' | sudo -S -p '' visudo -cf /tmp/pat-healer.sudoers >/dev/null 2>&1; then echo "SUDOERS-INVALID -> ABORT"; rm -f /tmp/pat-healer.sudoers; exit 2; fi
echo '__PW__' | sudo -S -p '' install -m 440 -o root -g root /tmp/pat-healer.sudoers /etc/sudoers.d/pat-healer && echo "SUDOERS-OK"
echo '__PW__' | sudo -S -p '' install -m 644 -o root -g root /tmp/pfh.service /etc/systemd/system/pat-fleet-healer.service && echo "SVC-OK"
echo '__PW__' | sudo -S -p '' install -m 644 -o root -g root /tmp/pfh.timer /etc/systemd/system/pat-fleet-healer.timer && echo "TMR-OK"
rm -f /tmp/pat-healer.sudoers /tmp/pfh.service /tmp/pfh.timer
# --- test NOPASSWD (reset-failed = harmless) ---
sudo -n $SYSCTL reset-failed pat-smart-radar 2>/dev/null && echo "NOPASSWD-OK" || echo "NOPASSWD-FAIL"
echo '__PW__' | sudo -S -p '' systemctl daemon-reload
echo '__PW__' | sudo -S -p '' systemctl enable --now pat-fleet-healer.timer >/dev/null 2>&1 && echo "TIMER-ENABLED"
echo "timer=$(systemctl is-active pat-fleet-healer.timer) enabled=$(systemctl is-enabled pat-fleet-healer.timer 2>/dev/null)"
echo "=== LIVE healer run #1 ==="
HEALER_DRY_RUN=0 $PYBIN /home/admin/.config/pat-smart/workers/healer.py 2>&1
echo "=== verify ==="
echo "radar=$(systemctl is-active pat-smart-radar) stream=$(systemctl is-active pat-smart-stream) redis=$(systemctl is-active redis-server) e1935=$(ss -tn state established 2>/dev/null|grep -c :1935)"
echo "STATION_NAME -> $(grep ^STATION_NAME ~/.config/pat-smart/.env)"
echo "netbird: $(netbird status 2>/dev/null|grep -iE 'Management:|Peers count')"
systemctl list-timers pat-fleet-healer.timer --no-pager 2>/dev/null | sed -n '1,2p'
