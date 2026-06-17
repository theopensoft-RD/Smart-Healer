RP=/home/admin/.config/pat-smart/workers/radar.py
echo "host=$(hostname)"
echo '__PW__' | sudo -S -p '' systemctl reset-failed pat-smart-radar 2>/dev/null
echo '__PW__' | sudo -S -p '' systemctl restart pat-smart-radar 2>/dev/null && echo "RESTARTED" || echo "SUDO-FAIL"
sleep 13
ST=$(systemctl is-active pat-smart-radar)
echo "radar=$ST"
echo "--- journal ---"
journalctl -u pat-smart-radar -n 24 --no-pager 2>/dev/null|grep -oiE "state [A-Za-z]+ -> [A-Za-z]+|circuit=(open|closed)|level=[0-9.-]+|ONLINE|FAULT|Traceback|MQTT Client Connect"|tail -6
if [ "$ST" != "active" ] || journalctl -u pat-smart-radar -n 40 --no-pager 2>/dev/null | grep -qi "Traceback"; then
  echo "!! RADAR UNHEALTHY -> ROLLBACK"
  cp "$RP.bak-2026-06-16" "$RP"
  echo '__PW__' | sudo -S -p '' systemctl restart pat-smart-radar 2>/dev/null
  sleep 8
  echo "post-rollback radar=$(systemctl is-active pat-smart-radar)"
else
  echo "PATCH-HEALTHY"
fi
echo "guard-present=$(grep -c 'circuit-open\" in str' "$RP")"
echo "netbird: $(netbird status 2>/dev/null|grep -iE 'Management:|Peers count')"
