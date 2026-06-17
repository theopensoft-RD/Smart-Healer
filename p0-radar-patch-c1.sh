RP=/home/admin/.config/pat-smart/workers/radar.py
PY=/home/admin/.local/share/pipx/venvs/pat-smart/bin/python
[ -x "$PY" ] || PY=python3
echo "host=$(hostname) py=$PY"
cp -n "$RP" "$RP.bak-2026-06-16" && echo "BACKUP-NEW $RP.bak-2026-06-16" || echo "BACKUP-EXISTS (keep original)"
cat > /tmp/_patch_radar.py <<'PYEOF'
p="/home/admin/.config/pat-smart/workers/radar.py"
s=open(p).read()
if 'circuit-open" in str(err)' in s:
    print("ALREADY-PATCHED"); raise SystemExit(0)
old='def on_read_failure(err):\n    global _failures, _circuit_open, _next_probe, reconnect_count\n    _failures += 1'
new=('def on_read_failure(err):\n'
     '    global _failures, _circuit_open, _next_probe, reconnect_count\n'
     '    if isinstance(err, TimeoutError) and "circuit-open" in str(err):\n'
     '        return  # P0 2026-06-16: circuit-open skip != real read failure;\n'
     '                # counting it re-armed _next_probe forever -> half-open never reached (stuck-open)\n'
     '    _failures += 1')
n=s.count(old)
if n!=1:
    print("ANCHOR-FAIL count=%d (abort, file untouched)"%n); raise SystemExit(2)
open(p,"w").write(s.replace(old,new,1))
print("PATCHED ok")
PYEOF
$PY /tmp/_patch_radar.py; rc=$?
if [ $rc -ne 0 ] && [ $rc -ne 0 ]; then :; fi
if [ $rc -eq 2 ]; then echo "ABORT anchor"; exit 2; fi
$PY -m py_compile "$RP" && echo "PYCOMPILE-OK" || { echo "PYCOMPILE-FAIL -> restore backup"; cp "$RP.bak-2026-06-16" "$RP"; $PY -m py_compile "$RP" && echo "restored-ok"; exit 3; }
echo "--- patched on_read_failure (249-267) ---"
sed -n '249,267p' "$RP"
rm -f /tmp/_patch_radar.py
echo "DONE-CALL1 (radar NOT restarted)"
