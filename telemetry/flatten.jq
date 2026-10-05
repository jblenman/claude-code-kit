# Flatten the collector's events file (one OTLP ExportLogsServiceRequest JSON per line) into one
# JSON object per event: resource attributes + record attributes + time + body.
#   jq -c -f flatten.jq /var/lib/otelcol-contrib/events/claude-code-events*.jsonl
# OTLP JSON encodes attribute values as {"stringValue": ...}, {"intValue": "123"} (a string!),
# {"doubleValue": 1.5}, {"boolValue": true}, {"arrayValue": {"values": [...]}}.

def unwrap:
  if type != "object" then .
  elif has("stringValue") then .stringValue
  elif has("intValue") then (.intValue | tonumber)
  elif has("doubleValue") then .doubleValue
  elif has("boolValue") then .boolValue
  elif has("arrayValue") then [.arrayValue.values[]? | unwrap]
  elif has("kvlistValue") then reduce (.kvlistValue.values[]? // empty) as $a ({}; .[$a.key] = ($a.value | unwrap))
  else . end;

def attrs: reduce (.[]? // empty) as $a ({}; .[$a.key] = ($a.value | unwrap));

.resourceLogs[]? as $r
| ($r.resource.attributes | attrs) as $res
| $r.scopeLogs[]?.logRecords[]?
| $res
  + (.attributes | attrs)
  + { time: (.timeUnixNano // .observedTimeUnixNano // null),
      body: (.body | unwrap) }
