"""Streamlit dashboard — GraphAgent Forge UI."""
import os
import streamlit as st
import httpx
import json

API_BASE = os.getenv("GRAPHAGENT_API", "http://localhost:8000") + "/api"

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
    st.caption("This is a secondary dashboard — the primary UI is served at the API root (GRAPHAGENT_API).")

    st.divider()

    # Graph stats
    try:
        stats = httpx.get(f"{API_BASE}/graph/stats", timeout=5).json()
        st.metric("Nodes", stats.get("nodes", 0))
        st.metric("Edges", stats.get("edges", 0))
        types = stats.get("entity_types", [])
        if types:
            st.caption(f"Entity types: {', '.join(types[:8])}")
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
    st.write("Feed URLs or text into the knowledge graph. Kimi AI extracts entities and relationships, Neo4j stores the graph.")

    ingest_mode = st.radio("Mode", ["URL", "Text"], horizontal=True)

    if ingest_mode == "URL":
        url = st.text_input("URL to ingest", placeholder="https://example.com/article")
        if st.button("🚀 Ingest URL", type="primary"):
            if url:
                with st.spinner("Fetching, extracting entities, building graph..."):
                    try:
                        result = httpx.post(
                            f"{API_BASE}/ingest/url",
                            json={"url": url},
                            timeout=120,
                        ).json()

                        if result.get("success"):
                            st.success(
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

    else:
        text = st.text_area(
            "Text to ingest",
            height=200,
            placeholder="Paste an article, notes, or any text...",
        )
        source = st.text_input("Source label", value="manual-input")
        if st.button("🚀 Ingest Text", type="primary"):
            if text:
                with st.spinner("Extracting entities, building graph..."):
                    try:
                        result = httpx.post(
                            f"{API_BASE}/ingest/text",
                            json={"text": text, "source": source},
                            timeout=120,
                        ).json()

                        if result.get("success"):
                            st.success(
                                f"✅ Ingested! {result['nodes']} nodes, {result['edges']} edges"
                            )
                        else:
                            st.error(result.get("error", "Ingestion failed"))
                    except Exception as e:
                        st.error(f"API error: {e}")
            else:
                st.warning("Enter some text first!")


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
                    ).json()

                    # Answer
                    st.subheader("📝 Answer")
                    st.write(result.get("answer", "No answer generated"))

                    # Context nodes
                    nodes = result.get("context_nodes", [])
                    if nodes:
                        st.subheader("🔗 Context Entities")
                        st.write(" → ".join(nodes))

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
                    timeout=30,
                ).json()

                if results:
                    st.write(f"Found {len(results)} entities:")
                    for entity in results:
                        with st.expander(f"**{entity['label']}** ({entity['type']})"):
                            st.write(entity.get("summary", "No summary"))
                            st.caption(f"ID: {entity['id']}")
                else:
                    st.info("No matching entities found. Try ingesting some data first!")

            except Exception as e:
                st.error(f"API error: {e}")
        else:
            st.warning("Enter a search term!")
