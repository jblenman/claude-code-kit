# Exact cost and tokens per machine per day from api_request events (see queries.md).
#   jq -c -f flatten.jq EVENTS... | jq -s -r -f cost-per-day.jq
# cost_usd is Claude Code's own estimate per request (the doc calls cost metrics approximations).
# Days are UTC dates taken from event.timestamp. Tokens = input + output + cache read + cache creation.
map(select(.["event.name"] == "api_request"))
| group_by([.machine, .["event.timestamp"][0:10]])
| ["machine", "day_utc", "requests", "cost_usd", "tokens_total", "input", "output", "cache_read", "cache_creation"],
  (.[] | [ .[0].machine, .[0]["event.timestamp"][0:10], length,
           (map(.cost_usd // 0) | add | . * 10000 | round / 10000),
           (map((.input_tokens // 0) + (.output_tokens // 0) + (.cache_read_tokens // 0) + (.cache_creation_tokens // 0)) | add),
           (map(.input_tokens // 0) | add), (map(.output_tokens // 0) | add),
           (map(.cache_read_tokens // 0) | add), (map(.cache_creation_tokens // 0) | add) ])
| @tsv
