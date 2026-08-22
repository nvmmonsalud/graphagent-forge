# GraphAgent Forge Sponsor Stack

*Source document for the seed corpus. Fixture `source_doc`: `GraphAgent Forge Sponsor Stack`.*

The **Sponsor Stack** is the set of four HackSprint sponsors GraphAgent Forge integrates:
Daytona, Nosana, Neo4j and Kimi AI. Three of the four sponsors provide a service with a local
fallback, so the app still runs when nothing is configured — which is what enables **Zero-Key
Demo Mode**, the configuration in which the application is demonstrated with no API keys at
all: seeded graph data, keyword retrieval, local verification and deterministic vectors.

## Daytona — sandboxed execution

**Daytona.** is the sandbox-execution sponsor. Among the sponsors that provide compute, Daytona
provides ephemeral sandboxes used to verify each freshly ingested subgraph. With no API key the
same check provides a local subprocess instead.

The integration surface is the **Daytona Sandbox SDK**: the client used to spin up an ephemeral
sandbox and run the graph-integrity check inside it. The graph payload is piped over standard
input rather than embedded in the command line, because argv is capped. When the SDK or the key
is missing, it falls back to the **Local Subprocess Fallback** — the same verification script in
a local subprocess with a thirty-second timeout, with the result recording which path was taken.

## Nosana — GPU embeddings

**NOSANA** is the decentralised GPU sponsor. Of the four sponsors, Nosana provides the embedding
endpoint that makes semantic retrieval possible; unconfigured, the client provides a
deterministic hash vector so nothing crashes. The GPU marketplace is settled on **Solana**, and
each submitted embedding job is paid for and accounted for on chain.

The **Nosana GPU Endpoint** is the configured embedding service. Vectors that come back
non-finite are rejected outright rather than written into the index. An unset endpoint URL routes
straight to the **Hash Pseudo-Embedding Fallback**: a deterministic 384-dimension unit-norm
vector derived from the text itself. It keeps the write path working, but it carries no semantic
signal, so retrieval must not treat it as one.

Both paths set the **Embedding Method Flag**, the recorded provenance of every stored vector. A
real vector is recorded as semantic; a stand-in vector is recorded as a fallback and excluded
from semantic retrieval. Retrieval and duplicate suggestion both read the flag to decide whether
a vector is trustworthy enough to reason from.

## Neo4j — graph storage

**Neo4J** is the graph-database sponsor. Neo4j provides the managed Aura instance the knowledge
graph lives in; it is the one dependency the sponsors provide that has no offline substitute, so
the app boots degraded without it.

**Neo4j Aura** is that managed service, holding entities, relationships, the normalised-label
index and the cosine vector index — including the embedding method flag, which is a property on
every embedded node. Zero-Key Demo Mode still requires it: storage is the one thing that cannot
be faked away.

## Kimi AI — extraction and answering

**Kimi AI** is the language-model sponsor. Kimi provides both entity extraction and the grounded
answer step. Unlike the other sponsors it deliberately provides no fallback, returning a
structured error when its key is missing.

Kimi is operated by **Moonshot AI**, the research company behind the Kimi model family. Moonshot
publishes the OpenAI-compatible endpoint the project talks to, sets the model identifier read
from configuration, and trains the **Kimi K2 Model** — the long-context model used for both
extraction and answering, whose identifier is resolved in exactly one place.

The **Structured Error Contract** is what Kimi surfaces in place of a fallback: a missing model
key produces a plain JSON error object that travels through the pipeline as data, never an SDK
exception escaping into a 500. Zero-Key Demo Mode relies on that contract, along with the local
subprocess fallback and the hash pseudo-embedding fallback.
