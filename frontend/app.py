"""Streamlit dashboard — GraphAgent Forge UI."""
import os

import httpx
import streamlit as st

API_BASE = os.getenv("GRAPHAGENT_API", "http://localhost:8000") + "/api"
# Matches the server's API_KEY when it is set; sent on ingest/ask calls.
HEADERS = {"X-API-Key": k} if (k := os.getenv("GRAPHAGENT_API_KEY")) else {}
if token := os.getenv("GRAPHAGENT_TOKEN"):
    HEADERS = {"Authorization": f"Bearer {token}"}
if workspace := os.getenv("GRAPHAGENT_WORKSPACE"):
    HEADERS["X-Workspace-ID"] = workspace


def md_escape(value) -> str:
    """Backslash-escape markdown so scraped/LLM text can't inject links or images.

    Used where Streamlit only takes markdown (expander labels, captions); free
    text goes through st.text, which never renders markdown.
    """
    return "".join("\\" + c if c in "\\`*_{}[]()<>#+-.!|~" else c for c in str(value))


st.set_page_config(
    page_title="GraphAgent Forge",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ------------------------------------------------------------------
# Sidebar
# ------------------------------------------------------------------
with st.sidebar:
    st.image("https://img.icons8.com/color/96/brain.png", width=64)
    st.title("🧠 GraphAgent Forge")
    st.caption("Turn scattered information into connected intelligence")
    st.caption("This is a secondary dashboard — the primary UI is served "
               "at the API root (GRAPHAGENT_API).")

    st.divider()

    # Graph stats
    try:
        stats = httpx.get(f"{API_BASE}/graph/stats", timeout=5, headers=HEADERS).json()
        st.metric("Nodes", stats.get("nodes", 0))
        st.metric("Edges", stats.get("edges", 0))
        types = stats.get("entity_types", [])
        if types:
            st.caption(f"Entity types: {md_escape(', '.join(types[:8]))}")
    except Exception:
        st.info("Start the API server to see graph stats")

    st.divider()
    st.caption("Powered by Kimi AI × Neo4j × Daytona × Nosana")
    st.caption("Built for Daytona HackSprint Tokyo 2026 🤙")


# ------------------------------------------------------------------
# Tabs
# ------------------------------------------------------------------
tab_ingest, tab_query, tab_search = st.tabs(["📥 Ingest", "🔍 Query", "🔎 Search"])


# ------------------------------------------------------------------
# Ingest Tab
# ------------------------------------------------------------------
with tab_ingest:
    st.header("📥 Ingest Data")
    st.write("Feed URLs or text into the knowledge graph. Kimi AI extracts entities "
             "and relationships, Neo4j stores the graph.")

    ingest_mode = st.radio("Mode", ["URL", "Text", "File"], horizontal=True)

    if ingest_mode == "URL":
        url = st.text_input("URL to ingest", placeholder="https://example.com/article")
        if st.button("🚀 Ingest URL", type="primary"):
            if url:
                with st.spinner("Fetching, extracting entities, building graph..."):
                    try:
                        result = httpx.post(
                            f"{API_BASE}/ingest/url?wait=true",
                            json={"url": url},
                            timeout=120,
                            headers=HEADERS,
                        ).json()

                        if result.get("success"):
                            st.success(
                                "✅ Already up to date" if result.get("unchanged") else
                                f"✅ Ingested! {result['nodes']} nodes, {result['edges']} edges"
                            )
                            ext = result.get("extraction", {})
                            if ext:
                                st.json(ext)
                        else:
                            st.error(result.get("error", "Ingestion failed"))
                    except Exception as e:
                        st.error(f"API error: {e}")
            else:
                st.warning("Enter a URL first!")

    elif ingest_mode == "Text":
        text = st.text_area(
            "Text to ingest",
            height=200,
            placeholder="Paste an article, notes, or any text...",
        )
        source = st.text_input("Source label", value="manual-input",
                               help="Reusing this name replaces the existing text source.")
        if st.button("🚀 Ingest Text", type="primary"):
            if text:
                with st.spinner("Extracting entities, building graph..."):
                    try:
                        result = httpx.post(
                            f"{API_BASE}/ingest/text?wait=true",
                            json={"text": text, "source": source},
                            timeout=120,
                            headers=HEADERS,
                        ).json()

                        if result.get("success"):
                            st.success(
                                "✅ Already up to date" if result.get("unchanged") else
                                f"✅ Ingested! {result['nodes']} nodes, {result['edges']} edges"
                            )
                        else:
                            st.error(result.get("error", "Ingestion failed"))
                    except Exception as e:
                        st.error(f"API error: {e}")
            else:
                st.warning("Enter some text first!")

    else:
        up = st.file_uploader("File to ingest", type=["pdf", "txt", "md"])
        source = st.text_input("Source label (optional)", value="",
                               help="Reusing a name or filename replaces that file source.")
        if st.button("🚀 Ingest File", type="primary"):
            if up:
                with st.spinner("Parsing file, extracting entities, building graph..."):
                    try:
                        up_type = up.type or "application/octet-stream"
                        result = httpx.post(
                            f"{API_BASE}/ingest/file?wait=true",
                            files={"file": (up.name, up.getvalue(), up_type)},
                            data={"source": source} if source else None,
                            timeout=120,
                            headers=HEADERS,
                        ).json()

                        if result.get("success"):
                            st.success(
                                "✅ Already up to date" if result.get("unchanged") else
                                f"✅ Ingested! {result['nodes']} nodes, {result['edges']} edges"
                            )
                        else:
                            st.error(result.get("error", "Ingestion failed"))
                    except Exception as e:
                        st.error(f"API error: {e}")
            else:
                st.warning("Choose a file first!")


# ------------------------------------------------------------------
# Query Tab
# ------------------------------------------------------------------
with tab_query:
    st.header("🔍 Query Knowledge Graph")
    st.write("Ask questions — GraphRAG retrieves context from the graph and reasons with Kimi AI.")

    question = st.text_input(
        "Your question",
        placeholder="What are the key relationships between...",
    )

    if st.button("🧠 Ask", type="primary"):
        if question:
            with st.spinner("Searching graph, reasoning with Kimi..."):
                try:
                    result = httpx.post(
                        f"{API_BASE}/ask",
                        json={"question": question},
                        timeout=120,
                        headers=HEADERS,
                    ).json()

                    # Answer
                    st.subheader("📝 Answer")
                    if result.get("status") == "llm_unavailable":
                        st.warning(
                            "LLM unavailable (KIMI_API_KEY missing or the request failed); "
                            "no answer generated."
                        )
                    elif result.get("status") == "insufficient_evidence":
                        st.warning(
                            "Insufficient current original source evidence. Reingest or rerun."
                        )
                    else:
                        st.text(result.get("answer", "No answer generated"))
                    if result.get("conflicts"):
                        st.warning("Conflicting evidence was found.")
                        st.json(result["conflicts"])
                    if result.get("citations"):
                        with st.expander("Original evidence references"):
                            st.json(result["citations"])
                            st.caption("Open the primary web UI to inspect current excerpts.")

                    # Context nodes
                    nodes = result.get("context_nodes", [])
                    if nodes:
                        st.subheader("🔗 Context Entities")
                        st.text(" → ".join(nodes))

                    # Sources
                    sources = result.get("sources", [])
                    if sources:
                        with st.expander("📊 Source Entities"):
                            st.json(sources)

                except Exception as e:
                    st.error(f"API error: {e}")
        else:
            st.warning("Ask a question first!")


# ------------------------------------------------------------------
# Search Tab
# ------------------------------------------------------------------
with tab_search:
    st.header("🔎 Search Entities")
    st.write("Find specific entities in the knowledge graph.")

    search_query = st.text_input("Search term", placeholder="Google, AI, Tokyo...")

    if st.button("🔎 Search", type="primary"):
        if search_query:
            try:
                results = httpx.post(
                    f"{API_BASE}/graph/search",
                    json={"query": search_query},
                    headers=HEADERS,
                    timeout=30,
                ).json()

                if results:
                    st.write(f"Found {len(results)} entities:")
                    for entity in results:
                        label = md_escape(entity["label"])
                        with st.expander(f"**{label}** ({md_escape(entity['type'])})"):
                            st.text(entity.get("summary", "No summary"))
                            st.caption(f"ID: {md_escape(entity['id'])}")
                else:
                    st.info("No matching entities found. Try ingesting some data first!")

            except Exception as e:
                st.error(f"API error: {e}")
        else:
            st.warning("Enter a search term!")
