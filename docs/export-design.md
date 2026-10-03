# Why the exporter works this way

Reviewed October 2, 2026. This is a source review of the integrations behind the Lens framework examples, not a claim that every framework was run in this test suite. The live acceptance tests here exercise Codex.

## What the other integrations do

| Integration | Relevant design |
| --- | --- |
| DeepAgents, LangGraph, LangChain | DeepAgents uses the LangChain/LangGraph runtime. [OpenInference's LangChain instrumentor](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-langchain/) observes its shared callbacks and emits OTel spans. The configured OTel processor controls export; sample code sometimes uses synchronous export for demonstration. [DeepAgents reference](https://reference.langchain.com/python/deepagents/graph/create_deep_agent). |
| OpenAI Agents SDK | Its [native batch processor](https://openai.github.io/openai-agents-python/ref/tracing/processors/) queues completed spans and exports on a background thread. The native exporter targets OpenAI; the [OpenInference integration](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-openai-agents/) supplies OTel-compatible tracing. |
| Claude Agent SDK | The [OpenInference instrumentor](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-claude-agent-sdk/) observes SDK execution and sends spans through the configured OTel provider. This is distinct from Claude Code's built-in telemetry. |
| Claude Code | [Built-in telemetry](https://code.claude.com/docs/en/monitoring-usage) has explicit content controls, background export, and a content-length limit. Its documented default content limit is 61,440 UTF-16 code units, with marked truncation. It does not depend on this plugin's SQLite queue. |
| CrewAI | [OpenInference](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-crewai/) instruments agent/tool execution and passes spans to OTel. Its event-listener mode warns against recording LLM spans twice when another instrumentor already records them. |
| LlamaIndex | Its [OpenInference instrumentor](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-llama-index/) also uses the supplied OTel tracer provider/exporter. |
| Pydantic AI | [Native instrumentation](https://pydantic.dev/docs/ai/integrations/logfire/) uses OTel and GenAI conventions. Its direct OTel example uses `BatchSpanProcessor`; Logfire is optional. |
| Google ADK | [Native tracing](https://github.com/google/adk-docs/blob/main/docs/observability/traces.md) supplies agent, model and tool spans using GenAI conventions, with configurable OTLP export. |
| Strands | [`setup_otlp_exporter`](https://strandsagents.com/docs/api/python/strands.telemetry.config/) installs an OTel batch processor and OTLP exporter. |
| Vercel AI SDK | [Telemetry](https://ai-sdk.dev/docs/ai-sdk-core/telemetry) exposes structured agent/model/tool spans and independent input/output recording controls. The OTel provider controls delivery. |
| OpenClaw | The official [diagnostics-otel plugin](https://docs.openclaw.ai/plugins/reference/diagnostics-otel) exports diagnostic spans over OTLP with explicit content settings. Diagnostic telemetry and full conversation content are separate concerns. |
| Hermes | The community [hermes-otel plugin](https://briancaffey.github.io/hermes-otel/configuration/batch-export) uses `BatchSpanProcessor`; ending a span queues it without waiting for the network. It also offers [content capture modes](https://github.com/briancaffey/hermes-otel). |
| OpenTelemetry | The [batch processor specification](https://opentelemetry.io/docs/specs/otel/trace/sdk/#batching-processor) specifies a bounded queue, scheduled export, and flush behavior. Its span-count limit does not itself guarantee a small request in bytes. [OTLP](https://opentelemetry.io/docs/specs/otlp/) also defines partial-success handling and explicitly acknowledges possible duplicate delivery. |

## Decision for Codex

Use the same separation: capture structured events locally, assemble spans, export small batches in the background. Codex's hooks are short-lived processes, so their queue must survive process exit. An in-memory SDK queue inside each hook would not provide that durability.

The plugin therefore keeps its small standard-library helper:

- One stable trace per Codex session, one immutable agent span per completed turn, and child spans for observed tools. Later messages add spans to the same trace.
- Text-only content: remove media, internal MCP transport metadata, and duplicate structured/text representations before persistence. Long fields retain a marked beginning/end excerpt, up to 128 KiB of UTF-8 text.
- Atomic local event files before database import, a single importer, short SQLite writes, and read-only status queries. A busy database delays import without blocking Codex or dropping the saved event.
- At most 512 KiB per serialized OTLP request. JSON escaping and envelope bytes count toward the limit. Individual oversize spans are shortened explicitly without changing their IDs.
- Durable acknowledgement per batch. A restart skips acknowledged batches. An ambiguous response triggers readback before any replay; partial success is never blindly retried.
- Error reporting independent of SQLite, plus a heartbeat so a stopped sender is visible.

These changes do not make hooks a full model-traffic API. Only content exposed by Codex's hooks is available. The plugin still cannot reconstruct hidden reasoning, all hosted tool internals, or nested subagent execution reliably.

If LiteLLM gains an explicit idempotent ingestion contract, use it to make ambiguous network failures automatically retryable. Stable span IDs alone do not prove that a backend deduplicates requests.
