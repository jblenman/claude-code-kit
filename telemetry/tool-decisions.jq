# Per-session tool-decision counts from flattened events (see queries.md).
#   jq -c -f flatten.jq EVENTS... | jq -s -r -f tool-decisions.jq
# Columns: machine, session.id, total, accepted, rejected, per-source counts, per-tool counts.
# Pass --arg since 2026-10-01 to restrict by event.timestamp (ISO 8601, string comparison).
map(select(.["event.name"] == "tool_decision"
           and (($ARGS.named.since // "") == "" or .["event.timestamp"] >= $ARGS.named.since)))
| group_by(.["session.id"])
| ["machine", "session", "decisions", "accept", "reject", "by_source", "by_tool"],
  (.[] | {
      machine: .[0].machine,
      session: .[0]["session.id"],
      total: length,
      accept: (map(select(.decision == "accept")) | length),
      reject: (map(select(.decision == "reject")) | length),
      by_source: (group_by(.source) | map({(.[0].source): length}) | add),
      by_tool:   (group_by(.tool_name) | map({(.[0].tool_name): length}) | add) }
    | [.machine, .session, .total, .accept, .reject, (.by_source | tojson), (.by_tool | tojson)])
| @tsv
