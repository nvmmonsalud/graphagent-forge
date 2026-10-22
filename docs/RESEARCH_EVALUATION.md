# Research evaluation slice

Baseline `1713286`. The expanded corpus has ten invented sources and nine
predeclared questions. It includes equal titles/ambiguous labels, conflicting
dates, Unicode offsets, a fact after character 50,000, entity-free excerpts,
English morphology, source filtering, an instruction embedded in untrusted text,
and an unsupported question. Relevant source IDs were declared before running
retrieval. Merely including an injection string does not establish resistance to
prompt injection; no provider receives these fixtures during this evaluation.

## Offline checks

```bash
.venv/bin/python -m pytest -q tests/test_research_evaluation.py tests/test_m2_evaluation.py
.venv/bin/python -m scripts.research_evaluation tests/fixtures/research_evaluation.json rankings.json
```

The scorer accepts JSON mapping every case ID to ranked source IDs. It reports
macro source recall@k and mean reciprocal rank over positive questions only,
and empty-result rate over unsupported questions separately. It deduplicates
sources before the cutoff and rejects incomplete runs, unknown sources and
source-filter leakage. Arithmetic tests use supplied rankings; their values are
not retrieval measurements. Empty results are not an answer abstention metric.

## Disposable database measurement

```bash
RESEARCH_DISPOSABLE_NEO4J=1 .venv/bin/python -m pytest -q -s -m integration tests/test_research_evaluation_integration.py
```

Set `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD` to a disposable Neo4j 5.26
instance first. This test writes and clears its `research_evaluation` workspace;
never point it at an existing research database. It uses actual indexed lexical
retrieval, without entities, embeddings, extraction or answer-provider calls.
The output records all per-question rankings and aggregate metrics. The expanded
corpus has no invented numeric pass threshold: commit the first measured baseline
before agreeing a future quality gate. Existing M2/M5 fixture gates remain intact.

The first actual run on 2026-10-21 used Neo4j Community 5.26.0, Python 3.11.15
and driver 6.4.1 against a fresh loopback database. Its supplied source rankings
and reproducible scores are in [research_baseline.json](research_baseline.json):
macro source recall@3 was 1.0 across eight positive questions, MRR@3 was 0.9375,
and the single unsupported question returned no hits. The near-duplicate ranked
ahead of the relevant planet source; perfect recall does not imply perfect
ranking, precision or answer quality. No answer provider ran.

No representative-document or live-provider answer-quality claim follows from
these synthetic tests. Semantic retrieval, human claim support, injection
resistance, real-world recall and cost remain unmeasured here. This slice changes
no application schema or runtime behavior; rollback removes only its tests,
fixture, scorer and documentation.
