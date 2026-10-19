"""Contract checks for bounded research UI; Chromium exercises full flows."""
from pathlib import Path

HTML = (Path(__file__).parents[1] / "frontend/index.html").read_text()


def test_source_scoped_questions_and_nonconfidence_diagnostics():
    assert 'id="question-source"' in HTML
    assert "source_id:document.getElementById('question-source').value" in HTML
    assert "Retrieval diagnostics (ranking signals, not confidence)" in HTML
    assert "new Option(source.title || source.source_doc, source.source_doc)" in HTML


def test_bulk_validation_precedes_submission_and_requires_editor():
    assert "commands.length > 10" in HTML
    assert "texts.reduce((n,t)=>n+t.text.length,0)>200000" in HTML
    assert "Text manifest must contain source/text objects." in HTML
    assert "button.hasAttribute('data-m5-write') && readerMode()" in HTML
    assert "researchEndpoint+'/bulk','POST',{commands}" in HTML
    assert "batch-cancel" in HTML


def test_schedule_is_explicit_and_reports_versions():
    assert 'data-research="refresh-create" data-m5-write' in HTML
    assert "interval_seconds<3600 || interval_seconds>2592000" in HTML
    assert "esc(item.previous_version || 'none')" in HTML
    assert "esc(item.current_version || 'none')" in HTML
    assert "File sources require re-upload." in HTML


def test_views_annotations_and_comparison_are_escaped_and_versioned():
    assert "payload.revision = researchActiveView.revision" in HTML
    assert "stale: excluded from answer grounding" in HTML
    assert "esc(note.note)" in HTML
    assert "esc(view.title)" in HTML
    assert "entity_ids:researchEntityIDs()" in HTML
    assert "ids.length > 5" in HTML
    assert "Restoring a view does not re-run its question." in HTML


def test_progressive_browse_is_bounded_and_retains_endpoint_closure():
    assert "nodes.size<500 || nodes.has(node.id)" in HTML
    assert "nodes.has(from) && nodes.has(to) && edges.size<1000" in HTML
    assert "done:!page.next_cursor || nodes.size>=500" in HTML
    assert "params.set('source_id',researchValue('question-source'))" in HTML


def test_workspace_clear_removes_all_new_panels_and_research_memory():
    assert "researchActiveView = null; researchBrowse =" in HTML
    for panel in ["research-batches", "research-refreshes", "research-views", "research-notes",
                  "research-compare", "research-browse", "research-message"]:
        assert f"'{panel}'" in HTML[HTML.index('function clearWorkspaceView()'):]
    for field in ["bulk-urls", "bulk-texts", "refresh-url", "investigation-title",
                  "research-entities", "annotation-evidence", "annotation-note"]:
        assert f"'{field}'" in HTML[HTML.index('function clearWorkspaceView()'):]
    assert "[data-m5-write]" in HTML
    assert "authRequestQueue.then(execute)" in HTML
