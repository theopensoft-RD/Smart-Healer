"""E3 - what the VEGAMET says about its own input, reported on transition. REPORT ONLY: nothing
here is something a restart could fix, and restarting hides the evidence (PIT001 2026-09-10, the
radar healer restarted a worker whose controller had left the network).

Two reads a tick, both cheap and read-only:
  1. is the controller on the LAN at all: ARP entry for HOST + TCP :502
     -> off the network  = radar.vegamet-off-network  (cable/power at the controller, or re-IP'd)
  2. the controller's web status page (no PIN; workers v2 read the same page for current_ma):
     the "Stromeingang" cell shows either the loop current or an error code
     -> E013/E014/E015    = radar.loop-open   (sensor current < 3.6 mA / line break: the VEGAPULS is
                            not on the loop - PIT043 read 0.00 m for a week this way, 2026-09-17..24)
     -> other E0xx        = radar.vegamet-error
     -> < 3.8 mA / > 20.5 = radar.loop-open / radar.loop-over
     -> in band           = ok (radar.loop-ok on recovery so the record clears)
The worker publishes the raw number; this is the field-facing verdict beside it."""
import re
from .base import Healer

_S = "loop"
LOOP_MIN_MA = 3.8
LOOP_MAX_MA = 20.5
# the cell is the text between the LAST row label and its "mA" unit (the page repeats the label as a
# section heading above the table: " Stromeingang Eingang Wert Einheit Stromeingang 7,044 mA "). Not
# every controller is set to German: PIT002 says " current input input reading dimension current input
# 16,106 mA " (found 2026-09-24 as a false loop-unreadable), so both labels are accepted, and a page
# with neither falls back to the workers' rule - the first "n,nnn mA" anywhere on it.
_LABEL = r"(?:Stromeingang|current\s+input)"
_CELL = re.compile(r"%s\s+((?:(?!%s).)*?)\s*mA\b" % (_LABEL, _LABEL), re.S | re.I)
_ANY_MA = re.compile(r"([0-9]+[.,][0-9]+)\s*mA\b")
_NUM = re.compile(r"^[0-9]+[.,][0-9]+$")                  # decimal comma on the VEGAMET 391, both languages
_ERR = re.compile(r"\bE\s?0?(\d{2,3})\b")   # evidence key is `err`, never `code`: ctx.event(code, **fields) owns that name


class VegametLoopHealer(Healer):
    name = "loop"

    def run(self, ctx):
        st = ctx.state_load(_S) or {}
        verdict, ev = self._judge(ctx)
        if verdict is None:
            return                                            # could not read anything this tick: say nothing
        if verdict == st.get("verdict"):
            return
        st["verdict"] = verdict
        ctx.state_save(_S, st)
        if verdict == "ok":
            if st.get("reported"):
                ctx.event("radar.loop-ok", **ev)
            return
        st["reported"] = True
        ctx.state_save(_S, st)
        ctx.event("radar." + verdict, **ev)

    def _judge(self, ctx):
        host = ctx.modbus_host
        neigh = self._neigh(ctx, host)
        if neigh in ("INCOMPLETE", "FAILED", "absent") and not ctx.tcp_up(host, 502, 3):
            return "vegamet-off-network", {"host": host, "arp": neigh}
        page = self._page(ctx, host)
        if page is None:
            return None, {}
        text = re.sub(r"<[^>]+>", " ", page)
        cell = self._input_cell(text)
        if cell is None:
            return "loop-unreadable", {"host": host}
        m = _ERR.search(cell)
        if m:
            code = "E%03d" % int(m.group(1))
            if code in ("E013", "E014", "E015"):
                return "loop-open", {"host": host, "err": code, "note": "sensor current < 3.6 mA or line break"}
            return "vegamet-error", {"host": host, "err": code}
        if not _NUM.match(cell):
            return "loop-unreadable", {"host": host, "cell": cell[:24]}
        ma = float(cell.replace(",", "."))
        if ma < LOOP_MIN_MA:
            return "loop-open", {"host": host, "ma": ma}
        if ma > LOOP_MAX_MA:
            return "loop-over", {"host": host, "ma": ma}
        return "ok", {"host": host, "ma": ma}

    @staticmethod
    def _input_cell(text):
        """The value cell of the current-input row ('Stromeingang' / 'current input'): the text
        between the row label and the unit 'mA' ("7,044" or "E 015"). A page in a third language
        falls back to the workers' rule - the first 'n,nnn mA' anywhere on it (a number only; an
        error code can only be read from a labelled row). None if there is neither."""
        m = _CELL.search(text)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
        m = _ANY_MA.search(text)
        return m.group(1) if m else None

    # --- probes (instance methods -> stubbable) ---
    def _neigh(self, ctx, host):
        rc, out, _ = ctx.sh("ip neigh show %s 2>/dev/null" % host, timeout=5)
        if rc != 0:
            return "unknown"
        if not out.strip():
            return "absent"
        for s in ("REACHABLE", "STALE", "DELAY", "PROBE", "INCOMPLETE", "FAILED", "PERMANENT"):
            if s in out:
                return s
        return "unknown"

    def _page(self, ctx, host):
        # page path prefix (e.g. '/049/') from where the root redirects to, never hard-coded -
        # the same rule as the workers' discover_current_prefix()
        rc, final, _ = ctx.sh("curl -s -m 4 -L -o /dev/null -w '%%{url_effective}' http://%s/" % host, timeout=8)
        if rc != 0:
            return None
        path = re.sub(r"^https?://[^/]*", "", (final or "").strip())
        prefix = path.rsplit("/", 1)[0] + "/" if "/" in path.strip("/") else "/"
        rc, body, _ = ctx.sh("curl -s -m 4 http://%s%sinput.htm" % (host, prefix), timeout=8)
        if rc != 0 or not body:
            return None
        return body
