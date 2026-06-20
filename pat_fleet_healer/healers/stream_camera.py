"""F4/F5/F6/F7/F9 - stream not pushing -> config repair (L3) + restart.
quote STATION_NAME (parens bug) · re-resolve a drifted/placeholder camera IP ·
force the camera to H.264 · fill __CAM_IP__. Camera absent -> escalate (technician).
The .env-mutating helpers are instance methods so the test suite can stub them."""
import os
import re
import time
from .base import Healer


class StreamCameraHealer(Healer):
    name = "stream"

    def run(self, ctx):
        svc = "pat-smart-stream"
        if ctx.svc_age(svc) < ctx.grace_s and ctx.svc_active(svc):
            return
        # not broken if it is actively pushing -> only enforce STATION_NAME quoting (idempotent)
        if ctx.svc_active(svc) and ctx.estab_1935() > 0:
            self._fix_station_name_quote(ctx)
            return
        name = self.name
        if not ctx.rate_ok(name):
            return ctx.escalate(name, "stream-repair-rate-exceeded")
        changed = self._fix_station_name_quote(ctx)
        cred = self._cam_cred(ctx)
        cur_ip = None
        m = re.search(r"@([0-9.]+):554", ctx.env.get("RTSP_URL", ""))
        if m:
            cur_ip = m.group(1)
        try:
            placeholder = "__CAM_IP__" in open(ctx.cfg.env_path).read()
        except Exception:
            placeholder = False
        need_cam = placeholder or (cur_ip and not ctx.tcp_up(cur_ip, 554))
        if need_cam:
            found = self._scan_554(ctx)
            if len(found) == 0:
                return ctx.escalate(name, "camera-absent", {"configured": cur_ip})
            if len(found) > 1:
                return ctx.escalate(name, "camera-ambiguous", {"found": found})
            newip = found[0]
            ctx.log("camera %s -> %s (drift/placeholder) + H.264" % (cur_ip, newip))
            self._set_codec_h264(ctx, newip, cred)
            self._repoint_cam(ctx, newip)
            changed = True
        if changed or not ctx.svc_active(svc) or ctx.estab_1935() == 0:
            ctx.rate_hit(name)
            ctx.restart(svc)

    # --- helpers (instance methods -> stubbable in tests) ---
    def _scan_554(self, ctx):
        return ctx.scan_port(554)

    def _cam_cred(self, ctx):
        m = re.search(r"rtsp://([^@]+)@", ctx.env.get("RTSP_URL", ""))
        return m.group(1) if m else "admin:"

    def _fix_station_name_quote(self, ctx):
        try:
            s = open(ctx.cfg.env_path).read()
        except Exception:
            return False
        m = re.search(r"^STATION_NAME=(.*)$", s, re.M)
        if not m:
            return False
        val = m.group(1)
        if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
            return False
        if not re.search(r"[()\s]", val):
            return False                                    # no special chars -> fine unquoted
        if ctx.dry_run:
            ctx.log("would quote STATION_NAME")
            return True
        self._backup_env(ctx)
        open(ctx.cfg.env_path, "w").write(s.replace("STATION_NAME=" + val, "STATION_NAME='" + val + "'", 1))
        ctx.log("quoted STATION_NAME (parens/space bug)")
        return True

    def _repoint_cam(self, ctx, newip):
        if ctx.dry_run:
            ctx.log("would re-point RTSP -> %s" % newip)
            return
        self._backup_env(ctx)
        s = open(ctx.cfg.env_path).read()
        s = re.sub(r"(rtsp://[^@]+@)[0-9.]+(:554)", r"\g<1>%s\g<2>" % newip, s)
        s = s.replace("__CAM_IP__", newip)
        open(ctx.cfg.env_path, "w").write(s)

    def _set_codec_h264(self, ctx, ip, cred):
        if ctx.dry_run:
            ctx.log("would set %s codec H.264" % ip)
            return
        b = "http://%s/ISAPI/Streaming/channels/101" % ip
        rc, cur, _ = ctx.sh("curl -sk -m8 --digest -u '%s' '%s'" % (cred, b))
        if "265" in cur or "HEVC" in cur.upper():
            new = re.sub(r"<videoCodecType>[^<]*</videoCodecType>",
                         "<videoCodecType>H.264</videoCodecType>", cur)
            open("/tmp/.cam_cfg", "w").write(new)
            ctx.sh("curl -sk -m8 --digest -u '%s' -X PUT -H 'Content-Type: application/xml' "
                   "--data-binary @/tmp/.cam_cfg '%s' >/dev/null 2>&1" % (cred, b))
            ctx.log("set %s codec -> H.264" % ip)

    def _backup_env(self, ctx):
        bak = ctx.cfg.env_path + ".bak-healer-" + time.strftime("%Y%m%d")
        if not os.path.exists(bak):
            try:
                ctx.sh("cp '%s' '%s'" % (ctx.cfg.env_path, bak))
            except Exception:
                pass
