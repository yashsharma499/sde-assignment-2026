import argparse
import logging
import sys

from .loop import Controller


def main():
    ap = argparse.ArgumentParser(prog="controller")
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--state-dir", default="state/run")
    ap.add_argument("--stop-after", default=None,
                    help="testing only: hard-exit (simulated crash) once sim time reaches e.g. 'D3 12:00'")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = None
    if a.stop_after:
        from .view import parse_when
        stop = parse_when(a.stop_after)
    Controller(a.url, a.state_dir, stop).run()


if __name__ == "__main__":
    main()
