# Per-session peak context and request count from api_request events: the same number a transcript
# audit reads from the `usage` block of each assistant message in ~/.claude/projects/**/*.jsonl.
#   jq -c -f flatten.jq EVENTS... | jq -s -r -f peak-context.jq
# Context of one request = input + cache_read + cache_creation (the docs' GenAI mapping); output
# tokens are summed separately. Main-thread requests only (query_source repl_main_thread), so
# subagent and compaction requests do not inflate the peak.
map(select(.["event.name"] == "api_request" and .query_source == "repl_main_thread"))
| group_by(.["session.id"])
| ["machine", "session", "first_request", "requests", "peak_context_tokens", "output_tokens", "models"],
  (.[] | [ .[0].machine, .[0]["session.id"], (map(.["event.timestamp"]) | min), length,
           (map((.input_tokens // 0) + (.cache_read_tokens // 0) + (.cache_creation_tokens // 0)) | max),
           (map(.output_tokens // 0) | add),
           (map(.model) | unique | join(",")) ])
| @tsv
