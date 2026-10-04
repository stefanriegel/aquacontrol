#!/bin/bash
# Runs on the machine with the GPUs (e.g. a VM with GPU passthrough): send GPU temperature/power/load to aquacontrol (display only).
# AQUACONTROL_URL, AQUACONTROL_TOKEN and AQUACONTROL_CA come from the unit's EnvironmentFile.
set -euo pipefail
sensors=$(nvidia-smi --query-gpu=index,temperature.gpu,power.draw,utilization.gpu \
                     --format=csv,noheader,nounits | python3 -B -c '
import json, sys

def number(text):
    try:
        return float(text)
    except ValueError:      # "[N/A]", "[Not Supported]", ...
        return None

out = []
for line in sys.stdin:
    fields = [v.strip() for v in line.split(",")]
    if len(fields) != 4:
        continue
    idx = fields[0]
    for suffix, label, value, unit in (
        ("", f"GPU {idx}", number(fields[1]), "°C"),
        ("_power", f"GPU {idx} Leistung", number(fields[2]), "W"),
        ("_util", f"GPU {idx} Last", number(fields[3]), "%"),
    ):
        if value is not None:
            out.append({"id": f"gpu{idx}{suffix}", "label": label, "value": value, "unit": unit})
print(json.dumps({"source": "llm-vm", "sensors": out}))
')
# The token goes to curl via stdin (-H @-), never through argv (visible in /proc/<pid>/cmdline).
curl -fsS --max-time 4 --cacert "$AQUACONTROL_CA" \
     -H @- -H "Content-Type: application/json" \
     -d "$sensors" "$AQUACONTROL_URL/api/external" >/dev/null <<< "Authorization: Bearer $AQUACONTROL_TOKEN"
