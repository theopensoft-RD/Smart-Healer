"""F4/F5/F6/F7/F9 - stream not pushing -> config repair (L3) + restart.
quote STATION_NAME (parens bug) · re-resolve a drifted/placeholder camera IP ·
re-resolve a drifted RTSP *path* after a camera brand swap · force the camera to
H.264 · fill __CAM_IP__. Camera absent -> escalate (technician).
The .env-mutating helpers are instance methods so the test suite can stub them.

Multi-brand (2026-07-31): the fleet is no longer Hikvision-only. A Dahua replacing a
Hikvision on the SAME ip keeps :554 open, so every reachability check passes while
ffmpeg 404s on the old path forever (real case: pit004, one full day undetected).
Reachability is therefore not enough - the path itself is validated, and the repair
endpoints are chosen per brand instead of assuming ISAPI."""
import os
import re
import time
from .base import Healer

# brand -> CANDIDATE main-stream RTSP paths, most-likely first. These are candidates,
# never assumptions: each one is probed and the first that actually answers is kept.
# Hikvision is listed /stream0-first because that is what this fleet is wired with and
# it works - a "canonical" path would overwrite 14 healthy nodes for no reason.
CAM_RTSP_PATHS = {
    "hikvision": ["/stream0", "/Streaming/Channels/101", "/h264/ch1/main/av_stream"],
    "dahua": ["/cam/realmonitor?channel=1&subtype=0", "/stream0"],
}


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
            cur_ip = newip
            changed = True
        # the camera answers on :554 - but does the configured PATH still exist? A camera
        # swapped for another brand at the same ip passes every check above while ffmpeg
        # 404s. Only an actual RTSP handshake can tell us; :554 being open cannot.
        if cur_ip and not self._rtsp_ok(ctx, ctx.env.get("RTSP_URL", "")):
            brand = self._detect_brand(ctx, cur_ip, cred)
            path = self._find_working_path(ctx, cur_ip, cred, brand)
            if not path:
                return ctx.escalate(name, "camera-path-unknown",
                                    {"ip": cur_ip, "brand": brand or "unknown"})
            ctx.log("camera %s is %s -> RTSP path %s" % (cur_ip, brand, path))
            self._set_codec_h264(ctx, cur_ip, cred)
            self._repoint_path(ctx, path)
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

    def _rtsp_ok(self, ctx, url):
        """A real RTSP DESCRIBE. :554 open proves a device is there, NOT that the
        configured path exists on it - that difference is the whole brand-swap bug."""
        url = (url or "").strip().strip("'\"")
        if not url:
            return False
        rc, _, _ = ctx.sh("ffprobe -rtsp_transport tcp -v error -show_entries stream=codec_name "
                          "-of default=nw=1 '%s'" % url, timeout=25)
        return rc == 0

    def _find_working_path(self, ctx, ip, cred, brand):
        """Probe candidate paths and return the first that ACTUALLY answers.
        Evidence, not a lookup table: an unknown brand still gets every candidate
        tried before we give up and call a technician."""
        cands = list(CAM_RTSP_PATHS.get(brand or "", []))
        for extra in CAM_RTSP_PATHS.values():          # unknown brand -> try them all
            for p in extra:
                if p not in cands:
                    cands.append(p)
        for p in cands:
            if self._rtsp_ok(ctx, "rtsp://%s@%s:554%s" % (cred, ip, p)):
                return p
        return None

    def _detect_brand(self, ctx, ip, cred):
        """Ask the camera what it is. Dahua answers magicBox.cgi, Hikvision answers ISAPI."""
        _, out, _ = ctx.sh("curl -sk -m8 --digest -u '%s' "
                           "'http://%s/cgi-bin/magicBox.cgi?action=getDeviceType'" % (cred, ip))
        if "type=" in (out or ""):
            return "dahua"
        _, out, _ = ctx.sh("curl -sk -m8 --digest -u '%s' "
                           "'http://%s/ISAPI/System/deviceInfo'" % (cred, ip))
        if "DeviceInfo" in (out or "") or "deviceType" in (out or ""):
            return "hikvision"
        return None

    def _repoint_path(self, ctx, path):
        if ctx.dry_run:
            ctx.log("would re-point RTSP path -> %s" % path)
            return
        self._backup_env(ctx)
        s = open(ctx.cfg.env_path).read()
        # replace everything after ':554' up to the closing quote / whitespace
        s = re.sub(r"(rtsp://[^@\s]+@[0-9.]+:554)[^'\"\s]*", lambda m: m.group(1) + path, s)
        open(ctx.cfg.env_path, "w").write(s)

    def _set_codec_h264(self, ctx, ip, cred):
        if ctx.dry_run:
            ctx.log("would set %s codec H.264" % ip)
            return
        if self._detect_brand(ctx, ip, cred) == "dahua":
            return self._set_codec_h264_dahua(ctx, ip, cred)
        b = "http://%s/ISAPI/Streaming/channels/101" % ip
        rc, cur, _ = ctx.sh("curl -sk -m8 --digest -u '%s' '%s'" % (cred, b))
        if "265" in cur or "HEVC" in cur.upper():
            new = re.sub(r"<videoCodecType>[^<]*</videoCodecType>",
                         "<videoCodecType>H.264</videoCodecType>", cur)
            tmp = "/tmp/.cam_cfg-%d" % os.getpid()          # per-process: /tmp is shared
            open(tmp, "w").write(new)
            ctx.sh("curl -sk -m8 --digest -u '%s' -X PUT -H 'Content-Type: application/xml' "
                   "--data-binary @%s '%s' >/dev/null 2>&1" % (cred, tmp, b))
            try:
                os.unlink(tmp)
            except Exception:
                pass
            # read back - a logged "set" that never took is how pit004 stayed black
            _, now, _ = ctx.sh("curl -sk -m8 --digest -u '%s' '%s'" % (cred, b))
            if "265" in (now or "") or "HEVC" in (now or "").upper():
                ctx.log("hikvision %s REFUSED H.264 - stream will be black" % ip)
            else:
                ctx.log("set %s codec -> H.264 (verified)" % ip)

    def _set_codec_h264_dahua(self, ctx, ip, cred):
        """RTMP/FLV cannot carry H.265, and these nodes run ENC=copy - an H.265 camera
        therefore streams "fine" while every viewer shows black. Forcing H.264 is the fix.

        The brackets MUST be percent-encoded: a raw 'Encode[0]' is rejected by the camera
        with an EMPTY body, so the old code logged success on a request that did nothing
        (real case: pit004, 2026-07-31). Read the value back before claiming anything."""
        base = "http://%s/cgi-bin/configManager.cgi" % ip
        _, cur, _ = ctx.sh("curl -sk -m8 --digest -u '%s' '%s?action=getConfig&name=Encode'"
                           % (cred, base))
        if "265" not in (cur or "") and "HEVC" not in (cur or "").upper():
            return
        _, out, _ = ctx.sh("curl -sk -m10 --digest -u '%s' '%s?action=setConfig"
                           "&Encode%%5B0%%5D.MainFormat%%5B0%%5D.Video.Compression=H.264"
                           "&Encode%%5B0%%5D.ExtraFormat%%5B0%%5D.Video.Compression=H.264'"
                           % (cred, base))
        _, now, _ = ctx.sh("curl -sk -m8 --digest -u '%s' '%s?action=getConfig&name=Encode'"
                           % (cred, base))
        if "MainFormat[0].Video.Compression=H.264" in (now or ""):
            ctx.log("set %s (dahua) codec -> H.264 (verified)" % ip)
        else:
            ctx.log("dahua %s REFUSED H.264 (reply=%r) - stream will be black" % (ip, (out or "")[:40]))

    def _backup_env(self, ctx):
        bak = ctx.cfg.env_path + ".bak-healer-" + time.strftime("%Y%m%d")
        if not os.path.exists(bak):
            try:
                ctx.sh("cp '%s' '%s'" % (ctx.cfg.env_path, bak))
            except Exception:
                pass
