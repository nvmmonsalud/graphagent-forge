"""Test-only child bootstrap: fence networking and dotenv before application import."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hosted.network_guard import install_network_guard  # noqa: E402


def main():
    port = int(os.environ["H1_APP_PORT"])
    install_network_guard(
        allowed_ports={int(p) for p in os.environ["H1_ALLOWED_PORTS"].split(",")},
        listen_port=port, report_path=Path(os.environ["H1_NETWORK_REPORT"]),
    )
    import dotenv

    dotenv.load_dotenv = lambda *_args, **_kwargs: False
    import uvicorn

    if barrier := os.getenv("H1_PUBLICATION_BARRIER"):
        import asyncio

        from src.agent.core import GraphAgent

        original_tail = GraphAgent._post_ingest

        async def publication_barrier(self, result, *args, **kwargs):
            if result.get("success") and not result.get("unchanged"):
                # This test-only pause is AFTER the actual graph transaction,
                # BEFORE the durable worker can persist its terminal result.
                Path(barrier).write_text("published")
                await asyncio.Event().wait()
            return await original_tail(self, result, *args, **kwargs)

        GraphAgent._post_ingest = publication_barrier

    from src.main import app

    # uvloop's native socket calls bypass CPython socket audit events. Keep the
    # test child's network operations on the audited standard-library path.
    uvicorn.run(app, host="127.0.0.1", port=port, workers=1, access_log=False,
                proxy_headers=False, loop="asyncio")


if __name__ == "__main__":
    main()
