"""Connected private HTTP/WS acceptance using a real app and disposable Neo4j.

Only the external completion service is scripted. These loopback HTTP tests do
not establish TLS/proxy readiness or real-provider answer quality.
"""
import json

import httpx
import pytest
from httpx_ws import WebSocketUpgradeError, connect_ws

pytestmark = pytest.mark.hosted

DOCUMENT = "Apricot built Cedar in 2042. Private marker: orchid-acceptance-739."
QUESTION = "What did Apricot build?"


def ingest(hosted, client, *, source="connected-note"):
    response = client.post("/api/ingest/text", json={"text": DOCUMENT, "source": source})
    assert response.status_code == 202, response.text
    assert response.json()["cancel_supported"] is True
    job_id = response.json()["job_id"]
    job = hosted.wait_job(client, job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["success"] is True, job
    return job


def test_authorization_precedes_provider_and_cookie_csrf(hosted):
    before = dict(hosted.provider.calls)
    with httpx.Client(base_url=hosted.origin, trust_env=False) as anonymous:
        response = anonymous.post("/api/ingest/text", content=b"not-json")
        assert response.status_code == 401
        assert anonymous.get("/api/graph/data").status_code == 401
        with pytest.raises(WebSocketUpgradeError) as denied:
            with connect_ws(hosted.origin + "/ws/graph", client=anonymous,
                            headers={"Origin": hosted.origin}):
                pytest.fail("Anonymous private WebSocket was accepted")
        assert denied.value.response.status_code == 403
    with hosted.client("reader") as reader:
        assert reader.post("/api/ingest/text", content=b"not-json").status_code == 403
        assert reader.post("/api/graph/clear").status_code == 403
        response = reader.get("/api/graph/data")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
    with httpx.Client(base_url=hosted.origin, trust_env=False) as cookie:
        assert hosted.login(cookie).status_code == 200
        assert cookie.post("/api/ingest/text", headers={"Origin": "https://hostile.invalid"},
                           json={"text": DOCUMENT}).status_code == 403
        assert cookie.post("/api/ingest/text", json={"text": DOCUMENT}).status_code == 403
        with pytest.raises(WebSocketUpgradeError) as denied:
            with connect_ws(hosted.origin + "/ws/graph", client=cookie,
                            headers={"Origin": "https://hostile.invalid"}):
                pytest.fail("Hostile-origin private WebSocket was accepted")
        assert denied.value.response.status_code == 403
    assert hosted.provider.calls == before


def test_text_and_upload_publish_real_graph_and_ws_progress(hosted):
    with httpx.Client(base_url=hosted.origin, trust_env=False, timeout=30,
                      headers={"Origin": hosted.origin}) as client:
        assert hosted.login(client).status_code == 200
        with connect_ws(hosted.origin + "/ws/graph", client=client,
                        headers={"Origin": hosted.origin}) as socket:
            job = ingest(hosted, client)
            job_id = job["id"]
            messages = []
            for _ in range(30):
                message = socket.receive_json(timeout=15)
                messages.append(message)
                if (message.get("type") == "job_update"
                        and message["job"]["id"] == job_id
                        and message["job"]["status"] == "succeeded"):
                    break
            else:
                pytest.fail("No terminal job update arrived over the real WebSocket")
        updates = [m["job"] for m in messages if m.get("type") == "job_update"]
        assert any(item["status"] == "running" for item in updates)
        assert "orchid-acceptance-739" not in json.dumps(messages)
        assert "orchid-acceptance-739" not in json.dumps(job)
        events = client.get(f"/api/jobs/{job_id}/events")
        assert events.status_code == 200
        assert events.json()["events"]
        assert "orchid-acceptance-739" not in events.text
        upload = client.post("/api/ingest/file", headers={"Origin": hosted.origin},
                             files={"file": ("note.md", DOCUMENT.encode(), "text/markdown")},
                             data={"source": "connected-upload"})
        assert upload.status_code == 202, upload.text
        final = hosted.wait_job(client, upload.json()["job_id"])
        assert final["status"] == "succeeded", final
        assert final["result"]["success"] is True
        graph = client.get("/api/graph/data").json()
        assert {"Apricot", "Cedar"} <= {node["label"] for node in graph["nodes"]}
        ids = {node["id"] for node in graph["nodes"]}
        assert graph["edges"]
        assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])
        assert "orchid-acceptance-739" not in json.dumps(graph)
        assert len(client.get("/api/sources").json()["sources"]) == 2
        assert hosted.provider.calls["extraction"] == 2


def test_citations_isolation_and_deletion_are_current(hosted):
    with hosted.client() as alpha, hosted.client("bob", "beta") as beta:
        job = ingest(hosted, alpha)
        source = job["result"]["source"]
        answer_response = alpha.post("/api/ask", json={"question": QUESTION, "source_id": source})
        assert answer_response.status_code == 200, answer_response.text
        answer = answer_response.json()
        assert answer["status"] == "answered", answer
        assert answer["citations"]
        citation = answer["citations"][0]
        current = alpha.get("/api/evidence/" + citation["id"])
        assert current.status_code == 200
        evidence = current.json()
        assert evidence["text"] == DOCUMENT[evidence["start"]:evidence["end"]]
        assert evidence["text"] == DOCUMENT
        assert evidence["source_id"] == source == citation["source_id"]
        assert evidence["version"] == citation["version"]
        assert "text" not in citation
        history = alpha.get("/api/history").json()["entries"]
        assert history and history[0]["question"] == QUESTION
        assert beta.get("/api/jobs/" + job["id"]).status_code == 404
        assert beta.get(f"/api/jobs/{job['id']}/events").status_code == 404
        assert beta.get("/api/evidence/" + citation["id"]).status_code == 404
        assert beta.get(f"/api/sources/{source}/evidence").json()["evidence"] == []
        assert beta.get("/api/graph/data", params={"source_doc": source}).json()["nodes"] == []
        assert beta.post(f"/api/history/{history[0]['id']}/save").status_code == 404
        assert beta.get("/api/history").json()["entries"] == []
        before = dict(hosted.provider.calls)
        scoped = beta.post("/api/ask", json={"question": QUESTION, "source_id": source})
        assert scoped.status_code == 200
        assert scoped.json()["status"] in {"no_context", "insufficient_evidence"}
        assert hosted.provider.calls == before
        assert beta.delete("/api/sources", params={"source_doc": source}).status_code == 404
        assert alpha.get("/api/evidence/" + citation["id"]).status_code == 200
        deleted = alpha.delete("/api/sources", params={"source_doc": source})
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted_nodes"] > 0
        assert alpha.get("/api/evidence/" + citation["id"]).status_code == 404
        again = alpha.post("/api/ask", json={"question": QUESTION, "source_id": source}).json()
        assert again["status"] in {"no_context", "insufficient_evidence"}
        assert again["citations"] == []
        assert alpha.get("/api/graph/data").json()["nodes"] == []


def test_provider_failure_keeps_published_evidence(hosted):
    with hosted.client() as client:
        job = ingest(hosted, client)
        before = client.get("/api/graph/data").json()
        hosted.provider.mode = "fail"
        response = client.post("/api/ask", json={"question": QUESTION}, timeout=30)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "llm_unavailable"
        assert response.json()["citations"] == []
        assert client.get("/api/graph/data").json() == before
        assert client.get(f"/api/sources/{job['result']['source']}/evidence").json()["evidence"]
