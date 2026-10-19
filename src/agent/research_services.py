"""Attach bounded research services to one already-authorized resource bundle."""
from src.agent.investigations import InvestigationStore
from src.agent.research_jobs import ResearchJobs


async def initialize_research(resources, *, identity=None, workspace_id=None):
    database = getattr(resources, 'database', None)
    investigations = InvestigationStore(database)
    await investigations.initialize()
    research = ResearchJobs(database, resources.jobs, resources.neo4j, resources.agent,
                            identity=identity, workspace_id=workspace_id)
    await research.initialize()
    try:
        await research.start()
    except BaseException:
        await research.shutdown()
        raise
    resources.investigations = investigations
    resources.research_jobs = research


async def shutdown_research(resources):
    service = getattr(resources, 'research_jobs', None)
    if service is not None:
        await service.shutdown()
