# M5 research and discovery contract

Baseline1148ab8, separate PR stacked on M4. Deliver roadmap mandatory slices1–3;
optional global/community synthesis remains conditional on demonstrated demand.
No reranker/paidjudge; Neo4j5.26 compatibility and M4 isolation are release gates.

## Ranked retrieval and browsing (retrieval agent)

Own neo4j_client.py,graphrag.py,workspace_scope.py,core.py and existing routes.py
source-specific AskRequest/ask dispatch ONLY, plusnewtests. Add indexed fulltext
EvidenceChunk search, safe server-built bounded Lucene terms, top40 lexical and
bounded entity-linked chunks with reciprocal rank fusion (k=60). Limit transfers,
finalthreechunks/24ktext, currentSourceversion checks unchanged. Ifindex unavailable
reportfallback and use bounded current lexical path. New index name included in
M4 SCHEMA_NAMES/literal rewriting. Semantic/hash distinctions remain explicit.
Candidate method/ranks/scores are diagnostics, never factualconfidence.

search_evidence(question,entity_ids=None,limit=6,source_id=None) filters source
BEFORE ranking/retrieval. GraphRAG query(question,source_id=None) passesfilter;
GraphAgent.ask and AskRequest optionalsource_id preservelegacycallwhenabsent.
Never allow other source excerpts in source-specific LLM prompts. Askresponse
add bounded retrieval_diagnostics/ranks; no rawexcerpts inpublicjob/history.

Add browse_graph(after_id=None,limit=100,source_id=None) bounded100nodes300edges
withendpointclosure andnext_id; exactnodeID deterministicordering scopedqueries.
API root/newresearchrouter wraps opaque boundedcursor. Fixed corpus measures
predeclared expected lexicalrecall and new stemming/termretrieval vs oldcontains;
report fixtureonly, no broadanswerqualityclaim.

## Bulk import and URL refresh (bulk agent)

NEW src/agent/research_jobs.py ResearchJobs(db,jobs,neo4j,actor_authorizer=None),
asyncinitialize/start/shutdown. db optionalNone supportsboundedmemorylocalmode.
Resourcegetter _resources(request) fromroutes; API new src/api/bulk_routes.py
routerprefix/api/research. Rootattaches resource.research_jobs lazily orstartup.
Useexisting durablejobs only forreplay/cancel; memory JobManager supported via
explicitdispatcher closure. Privatecommands BOTH_principal_id/_workspace_id added
SERVER-side from request.state auth; replaychecks currentmembership. No clientactor
fields trusted. Service receives command+publicparams alreadyvalidated.

POST/bulk accepts {commands:[versionedurl/text/filecommands]} max10; aggregate
JSON/body<=4MB existingHTTPcap, text<=200k aggregate, filedecoded<=3MBaggregate.
ValidateALLbefore firstsubmit, then returnperitem acceptedIDs/rejected queuefull
explicitly (partialacceptance is not atomic). Storebatchmetadataonly(max50batches)
with privateworkspaceIDs; no duplicate rawpayload store. POST/batches/{id}/cancel
cancelsallretainedactivejobs; GET/batches list/currentstatus bounded50, scoped.
Idempotency-Key optional derivebatchitemkeys safely <=128 (stablecanonicalhash).

POST/refreshes {url,interval_seconds,source_id?} editorcreates max20 schedules,
intervalmin3600max30days; exact URLidentity verified ifsource_id supplied. URL
stored privately ONLY because operator explicitly scheduledrefresh; never public
URLquerycredentials. GET/refreshes returns sanitizeddisplayURI and IDs/times/status,
lastjob/diffmetadata only. POST/refreshes/{id}/run queuesone; DELETEdisables+removes
storedURL; cancelsownedpendingjob ifactive. Schedulercheckinterval>=1s, persisted
next_due+lease/CASclaim so duplicateworkers don'tduplicatejobsubmissions; deterministic
idempotency key per scheduledrun. Oldmissedintervals coalesceone, no burstcatchup.
RefetchusesexistingSSRFDNSredirect/byteguards everytime. No defaultscheduledjobs.
Private scheduleauthor stored, reauthorized beforequeue/attempt. Revoke cancels/
disables nextwork. Retain onlymetadata previous_version/current_version/changed
from source+job; no originalhistoricaltextdiffclaim. File refresh requiresreupload.
IfsameURLwasalreadyingested unchangedversionsskipproviderextraction. Schedule
responsesmustnevercontain rawURLauthquery, commandtext/filebytes, oractorcredential.

## Investigations (investigation agent)

NEW src/agent/investigations.py InvestigationStore(db=None), asyncinitialize,
list/get/save/delete andboundedannotations methods as needed. max50savedviews;
title<=200/query<=4000/sourceID<=500/selectedentityIDs<=5/notes<=2000 eachmax20.
Store onlywhitelistedJSON, monotonic revision+CASupdate forconcurrentedits, sanitized
errors. MetadataIDs UUID; no arbitraryHTML/code. db optionalmemorydefaultlocal.
NEW src/api/investigation_routes.py routerprefix/api/research/investigations:
GETlist/get reader,POSTcreate/PUTupdate/DELETEeditor; guessedIDs return404within
workspace. Annotations {evidence_id,note} validateCURRENT excerpt server-side and
storeoriginalversion/source/offsetmetadata, noexcerpttext. Reading marksstale if
currentevidencemissing/replaced. Nothingstale becomesanswergrounding.
POST/api/research/compare readerread-only (rootmiddlewareREAD_POSTSadd); exactID
contexts up5/depth1 andsourceprovenance, explicitmissingIDs, no merging. GET
/api/research/browse wrapsgraphbrowse cursor<=1024 base64JSON boundedid; no userCypher.
Canownroutes ininvestigation_routes.py withappropriateprefix beforeinclude.

## UI, lifecycle and root integration

Rootowns main/runtime resource service initialization/shutdown, middleware read
classification, docs, config and finalchecks. Newservices perworkspace/fallback
local store initializedonce; shutdownscheduler BEFOREjobs/driverclose. Purgeknown
SQLitefile removesnewtables too. No newfilesecretpayloads. Privatecachefencesalso
covernewpanels; rolecontrols+serversideauth includeallnewroutes.
UIagentowns frontendindex/app andnewfrontendtests. Add source-specific question
select, rankeddiagnostics, bulkURL/textmanifestflow plusstatuscancel, explicit
URLrefresh optin/create/run/delete+versionmetadata, savedview/create/restore/delete,
selectedentitycomparison andevidencenotes, progressivebrowsebutton withbounded
rendering. Escapeallfields, private generationfences on everynewapiFetch, no raw
excerpts/history/credentials inlocalStorage. Reader maycompare/browse/restore but
notcreate/refresh/import/annotate. Documentworkflowbounds/unsupportedoutcomes.

## Gates

Meaningful endtoendflows, realSQLite reopen schedules/views, actualNeo4jfulltext
andnamespace sameIDisolation, predeclaredretrievalfixture, sourcefilterLLMinput,
SSRFrefresh/cancel/concurrency andmaliciousnotes tests; Chromiumflows; fullsuite,
lint, independent code/security exactcommitapproval. SeparateM4/M5PRs; reviewall
openPRs thenmergeM3->M4->M5 onlypassing exactheads/CI andverifyfinalmaincode.
