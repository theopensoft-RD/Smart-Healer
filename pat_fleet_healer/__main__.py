"""Entry point. One artifact, a few commands:
    python3 healer.pyz             -> run one healer tick (systemd oneshot timer)
    python3 healer.pyz collect     -> write an AI diagnostic bundle
    python3 healer.pyz selftest     -> DRY tick into a throwaway state dir; exit 0 iff
                                       the artifact imports+wires+runs (the self-update
                                       gate: a new pyz must pass this before it installs)
    python3 healer.pyz --version    -> print the version
"""
import sys


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "collect":
        from .tools.collect import main as collect_main
        collect_main()
    elif arg == "selftest":
        import os
        import tempfile
        os.environ["HEALER_DRY_RUN"] = "1"
        os.environ["HEALER_STATE_DIR"] = tempfile.mkdtemp(prefix="healer-selftest-")
        os.environ.setdefault("HEALER_ENV_PATH", os.path.expanduser("~/.config/pat-smart/.env"))
        from .config import Config
        from .healers.registry import default_registry
        try:
            cfg = Config()
            if not cfg.device_id:                      # selftest still proves import+wire even without a node .env
                cfg.device_id = "SELFTEST"
            from .context import production_context
            from .runner import run
            run(cfg=cfg)                               # a DRY tick to the temp state dir
            assert len(default_registry()) == 8
            print("selftest OK")
            sys.exit(0)
        except Exception as e:
            print("selftest FAIL: %r" % e)
            sys.exit(1)
    elif arg in ("--version", "version"):
        from . import __version__
        print(__version__)
    else:
        from .runner import main as tick
        tick()


if __name__ == "__main__":
    main()
