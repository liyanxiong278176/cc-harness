import asyncio
import argparse
from pathlib import Path

from cc_harness.webui import run_web_server

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=3081)
    args = parser.parse_args()
    try:
        asyncio.run(run_web_server(initial_cwd=Path('D:/ai八股/admin-system'), port=args.port, no_open=True))
    except KeyboardInterrupt:
        pass
