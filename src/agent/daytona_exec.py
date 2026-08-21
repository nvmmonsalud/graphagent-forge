"""Daytona sandbox executor — run agent code in isolated VMs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, ClassVar

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

    # Map language names to actual shell commands
    _LANG_COMMANDS: ClassVar[dict[str, str]] = {
        "python": "python3",
        "python3": "python3",
        "javascript": "node",
        "node": "node",
        "bash": "bash",
        "sh": "sh",
    }

    async def run_code(self, code: str, language: str = "python") -> dict[str, Any]:
        """Run code in a fresh Daytona sandbox."""
        if not self.client:
            return await self._local_fallback(code, language)

        sandbox = None
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
        finally:
            if sandbox is not None:
                try:
                    await sandbox.delete()
                    log.info("Sandbox %s deleted", sandbox.id)
                except Exception as e:
                    log.warning("Failed to delete sandbox %s: %s", sandbox.id, e)

    async def run_in_sandbox(
        self, sandbox_id: str, code: str, delete: bool = True
    ) -> dict[str, Any]:
        """Run code in an existing sandbox (for multi-step workflows)."""
        if not self.client:
            return await self._local_fallback(code, "python")

        sandbox = None
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
        finally:
            if delete and sandbox is not None:
                try:
                    await sandbox.delete()
                    log.info("Sandbox %s deleted", sandbox_id)
                except Exception as e:
                    log.warning("Failed to delete sandbox %s: %s", sandbox_id, e)

    async def _local_fallback(
        self,
        code: str,
        language: str,
        stdin_data: bytes | None = None,
    ) -> dict[str, Any]:
        """Local subprocess fallback when Daytona is unavailable.

        `stdin_data` is piped to the process instead of being embedded in the
        command line — argv is capped at ARG_MAX, stdin is not.
        """
        cmd = self._LANG_COMMANDS.get(language.lower(), "python3")

        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, "-c", code,
                stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=30
            )

            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(),
                "error": stderr.decode() if stderr else None,
                "method": "local",
            }
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"success": False, "error": "Execution timed out (30s)", "method": "local"}
        except Exception as e:
            return {"success": False, "error": str(e), "method": "local"}

    async def create_persistent_sandbox(self, name: str) -> dict[str, Any]:
        """Create a named sandbox that persists across calls."""
        if not self.client:
            return {"sandbox_id": f"local-{name}", "method": "local"}

        try:
            sandbox = await self.client.create()
            return {"sandbox_id": sandbox.id, "method": "daytona"}
        except Exception as e:
            log.error("Failed to create persistent sandbox: %s", e)
            return {"sandbox_id": f"local-{name}", "method": "local"}

    # ------------------------------------------------------------------
    # Graph verification in sandbox
    # ------------------------------------------------------------------

    # Validation script template — runs inside the sandbox.
    # `__GRAPH_LOAD__` is replaced by an expression yielding the graph dict:
    #   * Daytona path — the graph JSON embedded in the script (no argv limit).
    #   * local path   — `json.load(sys.stdin)`, since the script is passed to
    #                    `python3 -c` and argv is capped at ARG_MAX.
    _VERIFY_SCRIPT = r"""
import json, sys

graph_data = __GRAPH_LOAD__

nodes = graph_data.get("nodes", [])
edges = graph_data.get("edges", [])

node_ids = set()
for node in nodes:
    nid = node.get("id", "")
    if nid:
        node_ids.add(str(nid))

# Validate every edge's source & target exist as node IDs
orphan_edges = []
for edge in edges:
    src = str(edge.get("source", ""))
    tgt = str(edge.get("target", ""))
    if src not in node_ids:
        orphan_edges.append({"edge_source": src, "edge_target": tgt, "missing": "source"})
    if tgt not in node_ids:
        orphan_edges.append({"edge_source": src, "edge_target": tgt, "missing": "target"})

# Count nodes with a summary
nodes_with_summary = 0
for node in nodes:
    props = node.get("properties", {})
    summary = node.get("summary") or props.get("summary", "")
    if summary:
        nodes_with_summary += 1

node_count = len(nodes)
edge_count = len(edges)
quality_score = {
    "edge_node_ratio": round(edge_count / max(node_count, 1), 3),
    "nodes_with_summary_pct": round(100 * nodes_with_summary / max(node_count, 1), 1),
}

result = {
    "valid": len(orphan_edges) == 0,
    "node_count": node_count,
    "edge_count": edge_count,
    "orphan_edges": orphan_edges,
    "orphan_count": len(orphan_edges),
    "nodes_with_summary": nodes_with_summary,
    "quality_score": quality_score,
}

print(json.dumps(result))
"""

    async def verify_graph(self, graph_data: dict[str, Any]) -> dict[str, Any]:
        """Run graph-integrity validation inside a Daytona sandbox.

        Creates an ephemeral sandbox, executes a Python validation script,
        then tears the sandbox down.  Returns a dict with validation results
        plus metadata about how the check was run.
        """
        graph_json = json.dumps(graph_data)

        sandbox_id: str | None = None
        try:
            if not self.client:
                # Local fallback — no sandbox; stream the graph in over stdin so
                # large graphs can't blow past ARG_MAX on `python3 -c`.
                script = self._VERIFY_SCRIPT.replace("__GRAPH_LOAD__", "json.load(sys.stdin)")
                result = await self._local_fallback(
                    script, "python", stdin_data=graph_json.encode("utf-8")
                )
                if result["success"]:
                    parsed = json.loads(result["output"].strip().splitlines()[-1])
                    parsed["method"] = "local"
                    return parsed
                return {"valid": False, "error": result.get("error", "unknown"), "method": "local"}

            # --- Daytona path ---
            # The SDK ships the script as a file, so embedding the graph is safe.
            script = self._VERIFY_SCRIPT.replace(
                "__GRAPH_LOAD__", f"json.loads({json.dumps(graph_json)})"
            )
            last_error = None
            for attempt in range(2):
                try:
                    sandbox = await self.client.create()
                    sandbox_id = sandbox.id
                    response = await sandbox.process.code_run(script)
                    raw = response.result.strip().splitlines()[-1]
                    parsed = json.loads(raw)
                    parsed["method"] = "daytona"
                    parsed["sandbox_id"] = sandbox_id
                    return parsed
                except Exception as exc:
                    last_error = exc
                    log.warning("Verification attempt %d failed: %s", attempt + 1, exc)
                    if sandbox_id:
                        try:
                            sandbox = await self.client.get(sandbox_id)
                            await sandbox.delete()
                        except Exception:
                            pass
                        sandbox_id = None
            raise RuntimeError(f"Daytona verification failed after retries: {last_error}")

        except Exception as e:
            log.error("Graph verification failed: %s", e)
            return {
                "valid": False,
                "error": str(e),
                "method": "daytona" if self.client else "local",
            }

        finally:
            # Always clean up the sandbox
            if sandbox_id and self.client:
                try:
                    sandbox = await self.client.get(sandbox_id)
                    await sandbox.delete()
                    log.info("Verification sandbox %s deleted", sandbox_id)
                except Exception as cleanup_err:
                    log.warning("Could not delete sandbox %s: %s", sandbox_id, cleanup_err)
