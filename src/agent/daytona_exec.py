"""Daytona sandbox executor — run agent code in isolated VMs."""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Daytona SDK import (graceful fallback if not installed)
try:
    from daytona import AsyncDaytona, DaytonaConfig
    HAS_DAYTONA = True
except ImportError:
    HAS_DAYTONA = False
    log.warning("Daytona SDK not installed — using local fallback")


class DaytonaExecutor:
    """Execute code in isolated Daytona sandboxes."""

    def __init__(self):
        self.client = None
        if HAS_DAYTONA:
            api_key = os.getenv("DAYTONA_API_KEY", "")
            if api_key:
                config = DaytonaConfig(api_key=api_key)
                self.client = AsyncDaytona(config)
                log.info("Daytona client initialized")
            else:
                log.warning("DAYTONA_API_KEY not set — sandbox mode disabled")

    async def run_code(self, code: str, language: str = "python") -> dict[str, Any]:
        """Run code in a fresh Daytona sandbox."""
        if not self.client:
            return await self._local_fallback(code, language)

        try:
            sandbox = await self.client.create()
            response = await sandbox.process.code_run(code)

            return {
                "success": True,
                "output": response.result,
                "sandbox_id": sandbox.id,
                "method": "daytona",
            }
        except Exception as e:
            log.error("Daytona execution failed: %s", e)
            return await self._local_fallback(code, language)

    async def run_in_sandbox(self, sandbox_id: str, code: str) -> dict[str, Any]:
        """Run code in an existing sandbox (for multi-step workflows)."""
        if not self.client:
            return await self._local_fallback(code, "python")

        try:
            sandbox = await self.client.get(sandbox_id)
            response = await sandbox.process.code_run(code)
            return {
                "success": True,
                "output": response.result,
                "sandbox_id": sandbox_id,
                "method": "daytona",
            }
        except Exception as e:
            log.error("Daytona execution failed on %s: %s", sandbox_id, e)
            return {"success": False, "error": str(e), "method": "daytona"}

    async def _local_fallback(self, code: str, language: str) -> dict[str, Any]:
        """Local subprocess fallback when Daytona is unavailable."""
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(),
                "error": stderr.decode() if stderr else None,
                "method": "local",
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": "Execution timed out (30s)", "method": "local"}
        except Exception as e:
            return {"success": False, "error": str(e), "method": "local"}

    async def create_persistent_sandbox(self, name: str) -> dict[str, Any]:
        """Create a named sandbox that persists across calls."""
        if not self.client:
            return {"sandbox_id": f"local-{name}", "method": "local"}

        try:
            sandbox = await self.client.create(
                labels={"project": "graphagent-forge", "session": name}
            )
            return {"sandbox_id": sandbox.id, "method": "daytona"}
        except Exception as e:
            log.error("Failed to create persistent sandbox: %s", e)
            return {"sandbox_id": f"local-{name}", "method": "local"}
