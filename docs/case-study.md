# OTB Weather Market Resolution Case Study

# Purpose

We are building agents that can resolve prediction market questions from messy public evidence. This case study asks you to build a small version of the research and reconciliation layer for a hard market family: weather markets.

The goal is not to build a full oracle, blockchain indexer, database, UI, or settlement system. The goal is to show that you can take market text, source rules, timestamps, and public weather data, then return a correct, inspectable, and conservative resolution decision.

# OO context

At a high level, the Optimistic Oracle is an escalation-based lifecycle: a requester asks a question with ancillary data and economic parameters, a semi-permissionless proposer supplies an answer during liveness, and anyone can dispute before the window closes. Undisputed answers settle optimistically (once signed off by the OTB); disputed answers escalate to UMA tokenholder voting through the DVM before the resolved value is returned on-chain. The OTB sits around that flow by gathering evidence and weighing risk within the system. It's able to conditionally slow down settlements or resolve markets if its happy with the proposed resolution.

Some links for context:

* Managed OO implementation: [UMAprotocol/managed-oracle](https://github.com/UMAprotocol/managed-oracle).  
* UMA Oracle documentation: [How does UMA's Oracle work?](https://docs.uma.xyz/protocol-overview/how-does-umas-oracle-work)  
* Example live requests for Tokyo temp markets [here](https://explorer.uma.xyz/?q=Will+the+lowest+temperature+in+Tokyo+be&status=request_open&date_field=proposal_block_time&sort_by=proposal_time&sort_order=desc&page=1&page_size=100&group_neg_risk=true)

# The Problem

Weather markets look simple, but they are easy to get wrong. A good system has to choose the right station or source, handle local-day boundaries, distinguish daily high/low from intraday readings, normalize units, detect missing or late observations, and apply bracket or threshold rules exactly.

Your task is to design and implement a prototype agentic resolver for weather prediction markets. Given a market case, the resolver should search or scrape public source evidence, reconcile it against the market rules, and return one of: p1, p2, p3, p4, or unclear. It should also support replaying provided fixtures for deterministic evaluation.

# Example Markets

Use these as representative examples, not a fixed grading set:

* Wellington daily high: [https://polymarket.com/event/highest-temperature-in-wellington-on-june-1-2026](https://polymarket.com/event/highest-temperature-in-wellington-on-june-1-2026). Example market: "Will the highest temperature in Wellington be 22°C or higher on June 1?" The rules resolve against the Wunderground Wellington Intl Airport station (NZWN), not a generic city forecast, and require whole-degree Celsius after the next-day finality condition is satisfied.  
* Denver daily high: [https://polymarket.com/event/highest-temperature-in-denver-on-may-31-2026](https://polymarket.com/event/highest-temperature-in-denver-on-may-31-2026). Example market: "Will the highest temperature in Denver be between 68-69°F on May 31?" The rules point to Buckley Space Force Base station (KBKF) in Aurora, which is a good test of station mapping and city-label assumptions.  
* Seattle precipitation: [https://polymarket.com/event/precipitation-in-seattle-in-may](https://polymarket.com/event/precipitation-in-seattle-in-may). This is a contrast case using NOAA/NWS monthly summarized data rather than Wunderground, with a 2-decimal monthly precipitation total and explicit bracket/tie behavior.

# Why Wunderground Is Hard

Wunderground behaves more like a public web product than a stable resolution API. A resolver has to read the market's exact station URL and UI-dependent unit setting, then distinguish the daily high/low observation from partial intraday values. The data can change before finality. These markets often say they cannot resolve until the first next-day data point is published, and revisions before that cutoff count while later page changes do not.  The city name is not enough. "Denver" may resolve using a station in Aurora; "Seoul" may resolve using Incheon airport. The system should preserve uncertainty when the station, unit, precision, or finality condition cannot be verified.

# What You Will Build

* A runnable command or script that ingests the sample market package and produces one structured decision per market case.  
* A required live-retrieval mode that searches or scrapes public weather sources online when available, then records exactly what it queried and found.  
* A research/design document explaining what you built, how well it worked, tradeoffs, failure modes, and how you would scale it toward near-perfect accuracy.  
* An evaluation script or report showing performance on the provided visible cases, including misses, unclear cases, and source-quality failures.  
* A detailed README.md that explains installation, required environment variables or API keys, model/provider configuration, replay and live commands, expected outputs, and how to interpret failures or unclear cases.

# Input File

We will provide a zip file with one \`markets.json\` manifest plus a \`fixtures/\` directory. The submitted script should support two paths: replay mode reads only these files for deterministic grading, and live mode actively queries public weather sources online.

Each \`markets.json\` object should include \`case\_id\`, \`polymarket\_url\`, \`proposal\_tx\_hash\`, \`question\_data\`, and \`ancillary\_data\`. It may include lightweight source metadata or a fixture pointer if useful, but it should not embed raw source snapshots.

\`question\_data\` should include \`question\_id\`, \`market\_id\`, \`market\_slug\`, \`gamma\_slug\`, \`title\`, \`proposal\_time\`, and the p1/p2/p3/p4 mapping. This keeps the candidate focused on evidence resolution rather than blockchain indexing.

Captured source snapshots should live under \`fixtures/\`, not inside \`markets.json\`. For Wunderground, fixtures should contain the reduced observation payload from the exact station URL and date. For NOAA, fixtures should contain the captured monthly summary payload. The manifest may include a short \`notes\` field for known source quirks, but it should not include the gold answer.

Visible expected answers should live separately in \`gold\_visible/answers.json\`; hidden grading should use the same schema.

# Implementation Hints

You can choose the retrieval architecture. A good answer might use a custom MCP server or skill, browser automation, direct HTTP requests, or a purpose-built scraper for Wunderground station pages. The important requirement is not the specific tool; it is that live retrieval is inspectable, repeatable, and tied to the exact station, date, unit, and finality rule in the market.

The live path should store raw fetched evidence or normalized snapshots so the decision can be replayed later. Generic search snippets are not enough by themselves; the system should verify the underlying source page or source record.

# Expected Output

The submission should expose a single runnable entry point, for example \`python resolve.py \--input data/markets.json \--fixtures data/fixtures \--live\`, that returns structured JSON for each case with the final recommendation, confidence, evidence, and trace. The exact schema can be simple, but it should include:

* recommendation: p1, p2, p3, p4, or unclear.  
* confidence: a number from 0 to 1\.  
* evidence: the decisive observations or source records used.  
* source\_trace: what was queried or read, and what each source returned.  
* reasoning: a concise explanation tied to the market rules and time window.  
* review\_reason: why the case should remain unclear or require human review, if applicable.

# Evaluation

We care most about correctness, conservatism, traceability, and research judgment. A wrong confident p1 or p2 is worse than returning unclear on a genuinely ambiguous case.

Your solution will be evaluated on visible cases and a hidden set with similar structure. We will run the submitted project in our own environment using the README commands against provided sample packages and hidden packages with the same schema, so the setup and command surface should be explicit and reproducible. We will look at accuracy, false positive and false negative behavior, appropriate use of unclear, source selection, evidence quality, replayability, live retrieval quality, code clarity, and whether the system can be debugged from its trace. The write-up matters as much as the prototype: it should show what you built, how well it worked, what failed, and how you would improve the system if the goal were to approach 100% accuracy at production scale.

# Sample Data Package

Sample manifest: [sample markets.json](https://drive.google.com/file/d/1ffvKf1yNj2iR1MLZP0prxXDB4x7PZmaS/view?usp=drivesdk).

The linked file is an example markets.json manifest, not the full grading bundle. It gives candidates enough structure to understand the visible cases: real Polymarket and OO identifiers, title and outcome mappings, full ancillary data, proposal timing metadata, and source metadata for each authoritative weather station, date, unit, and measurement.

For deterministic evaluation, provide a zip package with:

* markets.json: manifest with the market cases. Keep expected answers out of this file; if proposal metadata is included for traceability, candidates should treat it as non-authoritative.  
* fixtures/: captured raw or normalized weather-source responses used by replay mode. The package can map cases to fixture files by \`case\_id\`, a small fixture manifest, or an optional \`fixture\_path\`; the market manifest should not embed raw source snapshots.  
* gold\_visible/answers.json: expected answers for visible examples, kept separate from markets.json.  
* schema/: input and output schema examples.  
* README.md: detailed setup and operator instructions. Include install steps, supported runtime versions, required environment variables, model/provider configuration, replay command, live command, expected output shape, fixture layout, troubleshooting notes, and a short design section covering problem framing, tradeoffs, lessons learned, and how you would extend the resolver.

Because candidates may have custom local setups, the package should be runnable from a clean checkout using the README commands. Evaluators should be able to point the submitted tool at either the visible sample package or a hidden package without editing code.  
Replay mode should be the deterministic grading anchor. Live retrieval is still required, but live weather pages can change or fail, so live mode should store the fetched evidence and explain any mismatch with fixture replay.

# Bonus: Live OTB Mode

As a bonus extension, add a live OTB mode that periodically pulls proposed Weather markets from the Oracle API. Use this Oracle API Weather proposed requests endpoint: [https://oracle.api.otb.uma.xyz/requests?tags\_any=Weather\&visible\_integrations=polymarket%2Cpredict-fun\&status=proposed\&date\_field=proposal\_block\_time\&sort\_by=proposal\_time\&sort\_order=desc\&page=1\&page\_size=100](https://oracle.api.otb.uma.xyz/requests?tags_any=Weather&visible_integrations=polymarket%2Cpredict-fun&status=proposed&date_field=proposal_block_time&sort_by=proposal_time&sort_order=desc&page=1&page_size=100). Polling it every five minutes should return the newest proposed Weather markets visible to the OTB/Oracle API for integrations such as Polymarket and Predict.Fun.  
These live markets correspond to the UMA Explorer Weather proposed view: [https://explorer.uma.xyz/?tags\_any=Weather\&status=proposed\&date\_field=proposal\_block\_time\&sort\_by=proposal\_time\&sort\_order=desc\&page=1\&page\_size=100\&group\_neg\_risk=true](https://explorer.uma.xyz/?tags_any=Weather&status=proposed&date_field=proposal_block_time&sort_by=proposal_time&sort_order=desc&page=1&page_size=100&group_neg_risk=true). The live mode should treat those rows as production cases, run the same resolver pipeline used for fixture cases, persist the fetched request payload and source evidence, and paper-propose recommendations so the team can compare the resolver's p1/p2/p3/p4/unclear decisions against real-time markets without affecting settlement.

# What Strong Work Looks Like

Strong submissions separate retrieval, normalization, reconciliation, and final decision logic. They make source authority explicit, handle units and time zones carefully, know when data is incomplete, produce useful traces, and include an evaluation harness. The strongest submissions also explain the next production steps: station/source registries, source snapshots, retry and finality policies, benchmark expansion, human review routing, and continuous error analysis. They do not rely on one giant prompt that always forces a yes or no answer.

Strong work should also treat observability as product logic, not an afterthought. The resolver should emit structured telemetry for every source query, normalization step, reconciliation decision, fallback, and human-review trigger; track source freshness, failure rates, confidence drift, unclear rates, and proposal/settlement outcomes; and make those traces easy to inspect in logs, dashboards, or replay reports so operators can detect silent source degradation and understand why a market resolved the way it did.

# AI Tooling

You may use AI tools, any frontier model, and any model-selection or routing logic you want. We care less about the specific provider and more about whether the system makes source-grounded, reproducible, inspectable decisions, documents its model assumptions, and degrades conservatively when model calls fail or disagree.

# Follow-Up Discussion

In the follow-up interview, we may give you a new weather market or a broken fixture and ask how you would debug or extend your resolver. We are especially interested in how you would prevent repeat mistakes in a production oracle system.