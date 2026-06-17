PW='__PW__'
sx(){ echo "$PW" | sudo -S -p '' "$@"; }
VPY=/home/admin/.local/share/pipx/venvs/pat-smart/bin/python; [ -x "$VPY" ] || VPY=python3
HEALER=/home/admin/.config/pat-smart/workers/healer.py
HP=/home/admin/.config/pat-smart/.env
RUN(){ HEALER_DRY_RUN=0 HEALER_GRACE_S=0 $VPY $HEALER 2>&1 | grep -iE "restart|escalate|repoint|camera|codec|drift|redis|inactive|L1|L2" ; }
echo "host=$(hostname)"
sx systemctl stop pat-fleet-healer.timer; rm -f ~/.local/state/pat-smart/healer-rate.json; echo "timer stopped, rate cleared"

echo "===== L1: radar liveness (stop radar -> healer restart) ====="
sx systemctl stop pat-smart-radar; echo "injected: radar=$(systemctl is-active pat-smart-radar)"
echo "-- healer tick --"; RUN
sleep 5; A=$(systemctl is-active pat-smart-radar); echo "RESULT radar=$A  -> $([ "$A" = active ] && echo PASS || echo FAIL)"

echo "===== L2: redis dependency cascade (stop redis -> restart redis + bounce deps) ====="
sx systemctl stop redis-server; echo "injected: redis=$(systemctl is-active redis-server)"
echo "-- healer tick --"; RUN
sleep 6; echo "RESULT redis=$(systemctl is-active redis-server) radar=$(systemctl is-active pat-smart-radar) stream=$(systemctl is-active pat-smart-stream)  -> $([ "$(systemctl is-active redis-server)" = active ] && echo PASS || echo FAIL)"

echo "===== L3: camera DHCP-drift config-repair (corrupt IP -> healer re-resolves) ====="
REAL=$(grep -m1 ^RTSP_URL $HP | grep -oE "@[0-9.]+:" | tr -d "@:"); echo "real cam=$REAL"
cp $HP $HP.bak-livetest
sed -i "s/@$REAL:554/@192.168.1.250:554/" $HP
echo "corrupted -> $(grep -m1 ^RTSP_URL $HP | grep -oE '@[0-9.]+:')"
sx systemctl stop pat-smart-stream; rm -f ~/.local/state/pat-smart/healer-rate.json
echo "-- healer tick (scan ~40s) --"; RUN
sleep 10
NEW=$(grep -m1 ^RTSP_URL $HP | grep -oE "@[0-9.]+:" | tr -d "@:")
ST=$(systemctl is-active pat-smart-stream)
echo "RESULT cam $REAL->250->$NEW stream=$ST  -> $([ "$NEW" != "192.168.1.250" ] && [ "$ST" = active ] && echo PASS || echo FAIL)"

echo "===== restore + re-enable timer ====="
[ "$NEW" = "192.168.1.250" ] && { echo "healer no-fix -> restore .env"; cp $HP.bak-livetest $HP; sx systemctl restart pat-smart-stream; }
for s in redis-server pat-smart-radar pat-smart-stream; do systemctl is-active $s >/dev/null || sx systemctl restart $s; done
sx systemctl start pat-fleet-healer.timer
rm -f $HP.bak-livetest
sleep 3
echo "FINAL timer=$(systemctl is-active pat-fleet-healer.timer) radar=$(systemctl is-active pat-smart-radar) stream=$(systemctl is-active pat-smart-stream) redis=$(systemctl is-active redis-server) e1935=$(ss -tn state established 2>/dev/null|grep -c :1935)"
echo "netbird: $(netbird status 2>/dev/null|grep -iE 'Management:|Peers count')"
