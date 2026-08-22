"""Daytona sandbox executor — run agent code in isolated VMs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, ClassVar

log = logging.getLogger(__name__)

# Client-facing error vocabulary for verify_graph's "could not run" shape.
# Mirrors the job queue's fixed-literal rule: raw exception text is logged
# server-side and must never reach a client.
VERIFY_ERROR_UNAVAILABLE = "verification unavailable"
VERIFY_ERROR_TIMEOUT = "verification timed out"

# Same rule for the structural-analytics run. Deliberately DISTINCT strings
# from the verify pair: a failed analytics run must never be mistakable for a
# failed integrity verification (they surface in different response fields).
ANALYTICS_ERROR_UNAVAILABLE = "analytics unavailable"
ANALYTICS_ERROR_TIMEOUT = "analytics timed out"

# Default cap on exact (Brandes) betweenness. O(n*m) — cheap on demo-sized
# graphs, quadratic pain past a few hundred nodes. Overridable per deployment
# via ANALYTICS_MAX_BETWEENNESS_NODES, clamped to a sane band.
DEFAULT_MAX_BETWEENNESS_NODES = 400
_MIN_BETWEENNESS_NODES = 1
_MAX_BETWEENNESS_NODES = 5000


def _elapsed_ms(started: float) -> int:
    """Milliseconds since a time.perf_counter() reading."""
    return int(round((time.perf_counter() - started) * 1000))


def _error_literal(exc: BaseException | None, unavailable: str, timeout: str) -> str:
    """Map an execution failure onto a two-literal client vocabulary."""
    while exc is not None:
        if isinstance(exc, TimeoutError):
            return timeout
        exc = exc.__cause__
    return unavailable


def _verify_error_literal(exc: BaseException | None) -> str:
    """Map an execution failure onto verify_graph's two-literal vocabulary."""
    return _error_literal(exc, VERIFY_ERROR_UNAVAILABLE, VERIFY_ERROR_TIMEOUT)


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

        # Read once at construction: the budget is a deployment property, not
        # a per-request one. A non-numeric value falls back to the default.
        raw_budget = os.getenv("ANALYTICS_MAX_BETWEENNESS_NODES", "")
        try:
            budget = int(raw_budget) if raw_budget.strip() else DEFAULT_MAX_BETWEENNESS_NODES
        except ValueError:
            log.warning(
                "Invalid ANALYTICS_MAX_BETWEENNESS_NODES=%r — using %d",
                raw_budget, DEFAULT_MAX_BETWEENNESS_NODES,
            )
            budget = DEFAULT_MAX_BETWEENNESS_NODES
        self.betweenness_max = max(
            _MIN_BETWEENNESS_NODES, min(budget, _MAX_BETWEENNESS_NODES)
        )

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
        except TimeoutError:
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

    # ------------------------------------------------------------------
    # Shared sandbox runner
    # ------------------------------------------------------------------

    async def _run_graph_script(
        self,
        template: str,
        graph_data: dict[str, Any],
        *,
        err_unavailable: str,
        err_timeout: str,
        label: str,
    ) -> dict[str, Any]:
        """Run a stdlib-only graph script over `graph_data`, in a sandbox or locally.

        `template` must contain the `__GRAPH_LOAD__` token and print a single
        JSON object as its last stdout line. The token is substituted per path:

        * Daytona — the graph JSON embedded in the script; the SDK ships the
          script as a file, so there is no argv limit to respect.
        * local   — ``json.load(sys.stdin)``; the script goes to ``python3 -c``
          and argv is capped at ARG_MAX, so the payload rides stdin instead.

        Returns exactly one of two shapes, and every caller inherits both:

        * ran    — the script's own dict plus ``ok: True``, ``method`` and
          ``duration_ms`` (plus ``sandbox_id``/``boot_ms`` on the Daytona path).
        * failed — ``{"ok": False, "error": <literal>, "method", "duration_ms"}``
          and **no** domain keys at all, so a run that never produced a verdict
          can never be read as one. `error` is one of the two caller-supplied
          literals; the real exception goes to the log only.
        """
        started = time.perf_counter()
        method = "daytona" if self.client else "local"
        graph_json = json.dumps(graph_data)

        sandbox_id: str | None = None
        try:
            if not self.client:
                # Local fallback — no sandbox; stream the graph in over stdin so
                # large graphs can't blow past ARG_MAX on `python3 -c`.
                script = template.replace("__GRAPH_LOAD__", "json.load(sys.stdin)")
                result = await self._local_fallback(
                    script, "python", stdin_data=graph_json.encode("utf-8")
                )
                if result["success"]:
                    parsed = json.loads(result["output"].strip().splitlines()[-1])
                    parsed["ok"] = True
                    parsed["method"] = "local"
                    parsed["duration_ms"] = _elapsed_ms(started)
                    return parsed

                raw_error = result.get("error") or "unknown"
                log.error("Local graph %s failed: %s", label, raw_error)
                return {
                    "ok": False,
                    "error": (
                        err_timeout
                        if "timed out" in str(raw_error).lower()
                        else err_unavailable
                    ),
                    "method": "local",
                    "duration_ms": _elapsed_ms(started),
                }

            # --- Daytona path ---
            # The SDK ships the script as a file, so embedding the graph is safe.
            script = template.replace(
                "__GRAPH_LOAD__", f"json.loads({json.dumps(graph_json)})"
            )
            last_error: BaseException | None = None
            for attempt in range(2):
                try:
                    boot_started = time.perf_counter()
                    sandbox = await self.client.create()
                    boot_ms = _elapsed_ms(boot_started)
                    sandbox_id = sandbox.id
                    response = await sandbox.process.code_run(script)
                    raw = response.result.strip().splitlines()[-1]
                    parsed = json.loads(raw)
                    parsed["ok"] = True
                    parsed["method"] = "daytona"
                    parsed["sandbox_id"] = sandbox_id
                    parsed["boot_ms"] = boot_ms
                    parsed["duration_ms"] = _elapsed_ms(started)
                    return parsed
                except Exception as exc:
                    last_error = exc
                    log.warning(
                        "%s attempt %d failed: %s", label.capitalize(), attempt + 1,
                        exc, exc_info=True,
                    )
                    if sandbox_id:
                        try:
                            sandbox = await self.client.get(sandbox_id)
                            await sandbox.delete()
                        except Exception:
                            pass
                        sandbox_id = None

            log.error("Daytona %s failed after retries", label, exc_info=last_error)
            return {
                "ok": False,
                "error": _error_literal(last_error, err_unavailable, err_timeout),
                "method": "daytona",
                "duration_ms": _elapsed_ms(started),
            }

        except Exception as e:
            log.exception("Graph %s failed", label)
            return {
                "ok": False,
                "error": _error_literal(e, err_unavailable, err_timeout),
                "method": method,
                "duration_ms": _elapsed_ms(started),
            }

        finally:
            # Always clean up the sandbox
            if sandbox_id and self.client:
                try:
                    sandbox = await self.client.get(sandbox_id)
                    await sandbox.delete()
                    log.info("%s sandbox %s deleted", label.capitalize(), sandbox_id)
                except Exception as cleanup_err:
                    log.warning("Could not delete sandbox %s: %s", sandbox_id, cleanup_err)

    async def verify_graph(self, graph_data: dict[str, Any]) -> dict[str, Any]:
        """Run graph-integrity validation inside a Daytona sandbox.

        Creates an ephemeral sandbox, executes a Python validation script,
        then tears the sandbox down.

        Returns exactly one of two shapes:

        * ran   — ``{"ok": True, "valid": bool, ..., "method": ..., "duration_ms": int}``
          plus ``boot_ms``/``sandbox_id`` on the Daytona path.
        * failed — ``{"ok": False, "error": <literal>, "method": ..., "duration_ms": int}``

        The failure shape carries **no** ``valid`` key: its absence is the
        machine-readable "the check could not run" signal, so an execution
        failure can never be mistaken for a graph-integrity verdict.  ``error``
        is one of two literals; the real exception goes to the log only.
        """
        return await self._run_graph_script(
            self._VERIFY_SCRIPT,
            graph_data,
            err_unavailable=VERIFY_ERROR_UNAVAILABLE,
            err_timeout=VERIFY_ERROR_TIMEOUT,
            label="verification",
        )

    # ------------------------------------------------------------------
    # Structural graph analytics in sandbox
    # ------------------------------------------------------------------

    # Structural-metrics script — stdlib only (`json`, `sys`): neither the
    # Daytona image nor the local interpreter is guaranteed numpy/networkx.
    #
    # Two substitution tokens:
    #   * `__GRAPH_LOAD__`      — see `_run_graph_script`.
    #   * `__BETWEENNESS_MAX__` — the exact-betweenness node budget.
    #
    # Edges are treated as UNDIRECTED: this is a knowledge graph, and the
    # stored direction is just the order the LLM asserted the triple in, which
    # would otherwise skew every structural metric. Self-loops and parallel
    # edges are folded away into an adjacency set first.
    #
    # Bounds are enforced INSIDE the script so both execution paths agree on
    # what got computed rather than the caller second-guessing the sandbox.
    _ANALYTICS_SCRIPT = r"""
import json, sys

graph_data = __GRAPH_LOAD__
BETWEENNESS_MAX = __BETWEENNESS_MAX__

# Hard ceilings for the superlinear metrics. Components stay in scope past
# them: BFS is O(n+m) and never the thing that blows up.
MAX_NODES = 5000
MAX_EDGES = 50000
TOO_LARGE = "graph too large for structural metrics"

# Emit generously; the caller trims to its own `top`.
TOP_K = 50
PAGERANK_ITERATIONS = 20
DAMPING = 0.85

nodes = graph_data.get("nodes") or []
edges = graph_data.get("edges") or []

# --- build a deduped undirected adjacency set -------------------------
meta = {}
order = []
for node in nodes:
    nid = node.get("id")
    if nid is None or nid == "":
        continue
    nid = str(nid)
    if nid in meta:
        continue
    meta[nid] = {
        "id": nid,
        "label": node.get("label") or nid,
        "type": node.get("type") or "",
    }
    order.append(nid)

adj = dict((nid, set()) for nid in order)
pairs = set()
for edge in edges:
    src = str(edge.get("source", ""))
    tgt = str(edge.get("target", ""))
    if src not in adj or tgt not in adj or src == tgt:
        continue  # dangling endpoint or self-loop
    pair = (src, tgt) if src <= tgt else (tgt, src)
    if pair in pairs:
        continue  # parallel edge
    pairs.add(pair)
    adj[src].add(tgt)
    adj[tgt].add(src)

n = len(order)
m = len(pairs)


def entries(scores):
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_K]
    out = []
    for nid, score in ranked:
        item = dict(meta[nid])
        item["score"] = round(score, 5)
        out.append(item)
    return out


# --- weakly connected components (iterative BFS; recursion would blow the
# stack on a deep chain, and knowledge graphs grow long chains) ---------
seen = set()
sizes = []
for start in order:
    if start in seen:
        continue
    seen.add(start)
    frontier = [start]
    size = 0
    while frontier:
        nxt = []
        for u in frontier:
            size += 1
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    nxt.append(v)
        frontier = nxt
    sizes.append(size)
sizes.sort(reverse=True)

result = {
    "node_count": n,
    "edge_count": m,
    "components": {
        "count": len(sizes),
        "largest": sizes[0] if sizes else 0,
        "sizes": sizes[:10],
    },
}

if n > MAX_NODES or m > MAX_EDGES:
    # Components above are still real; everything else is refused by budget.
    result["skipped"] = True
    result["reason"] = TOO_LARGE
    result["top_pagerank"] = []
    result["top_betweenness"] = []
    result["avg_clustering"] = 0.0
    result["betweenness_reason"] = TOO_LARGE
    print(json.dumps(result))
    sys.exit(0)

result["skipped"] = False

# --- PageRank (undirected, uniform teleport, dangling mass redistributed) ---
pagerank = {}
if n:
    initial = 1.0 / n
    pagerank = dict((nid, initial) for nid in order)
    dangling_nodes = [nid for nid in order if not adj[nid]]
    for _ in range(PAGERANK_ITERATIONS):
        dangling = sum(pagerank[nid] for nid in dangling_nodes)
        base = (1.0 - DAMPING) / n + DAMPING * dangling / n
        nxt_rank = dict((nid, base) for nid in order)
        for u in order:
            degree = len(adj[u])
            if not degree:
                continue
            share = DAMPING * pagerank[u] / degree
            for v in adj[u]:
                nxt_rank[v] += share
        pagerank = nxt_rank

result["top_pagerank"] = entries(pagerank)

# --- betweenness (exact Brandes, unweighted, undirected) -------------------
betweenness_reason = None
if n > BETWEENNESS_MAX:
    betweenness_reason = (
        "skipped: " + str(n) + " nodes exceeds the " + str(BETWEENNESS_MAX)
        + "-node exact-betweenness budget"
    )
    result["top_betweenness"] = []
else:
    betweenness = dict((nid, 0.0) for nid in order)
    for s in order:
        stack = []
        preds = dict((nid, []) for nid in order)
        sigma = dict((nid, 0.0) for nid in order)
        dist = dict((nid, -1) for nid in order)
        sigma[s] = 1.0
        dist[s] = 0
        queue = [s]
        head = 0
        while head < len(queue):
            v = queue[head]
            head += 1
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = dict((nid, 0.0) for nid in order)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                betweenness[w] += delta[w]
    # Undirected: every shortest path is walked from both endpoints.
    for nid in betweenness:
        betweenness[nid] /= 2.0
    result["top_betweenness"] = entries(betweenness)

result["betweenness_reason"] = betweenness_reason

# --- average local clustering coefficient (degree >= 2 only) --------------
total = 0.0
counted = 0
for u in order:
    neighbors = adj[u]
    degree = len(neighbors)
    if degree < 2:
        continue  # coefficient is undefined, not zero
    links = 0
    neighbor_list = list(neighbors)
    for i in range(len(neighbor_list)):
        a = neighbor_list[i]
        for j in range(i + 1, len(neighbor_list)):
            if neighbor_list[j] in adj[a]:
                links += 1
    total += 2.0 * links / (degree * (degree - 1))
    counted += 1

result["avg_clustering"] = round(total / counted, 5) if counted else 0.0

print(json.dumps(result))
"""

    async def analyze_graph(self, graph_data: dict[str, Any]) -> dict[str, Any]:
        """Compute structural graph metrics in a sandbox (or locally).

        Neither GDS nor APOC is available (plain Neo4j / Aura), so components,
        PageRank, betweenness and clustering are computed in Python from the
        graph payload rather than in the database.

        Same two-shape envelope as `verify_graph`, with its own error literals:

        * ran    — ``{"ok": True, "components": {...}, "top_pagerank": [...],
          "top_betweenness": [...], "avg_clustering": float,
          "betweenness_reason": str|None, "method", "duration_ms", ...}``
        * failed — ``{"ok": False, "error": <literal>, "method", "duration_ms"}``
          with no metric keys whatsoever.
        """
        script = self._ANALYTICS_SCRIPT.replace(
            "__BETWEENNESS_MAX__", str(self.betweenness_max)
        )
        return await self._run_graph_script(
            script,
            graph_data,
            err_unavailable=ANALYTICS_ERROR_UNAVAILABLE,
            err_timeout=ANALYTICS_ERROR_TIMEOUT,
            label="analytics",
        )
