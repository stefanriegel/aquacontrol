#!/bin/bash
# Runs in VM 103: send GPU temperatures/power/utilisation to aquacontrol (display only).
# AQUACONTROL_URL, AQUACONTROL_TOKEN and AQUACONTROL_CA come from the unit's EnvironmentFile.
set -euo pipefail
sensors=$(nvidia-smi --query-gpu=index,temperature.gpu,power.draw,utilization.gpu \
                     --format=csv,noheader,nounits | python3 -c '
import json, sys
out = []
for line in sys.stdin:
    idx, temp, power, util = [v.strip() for v in line.split(",")]
    out.append({"id": f"gpu{idx}", "label": f"GPU {idx}", "value": float(temp), "unit": "°C"})
    out.append({"id": f"gpu{idx}_power", "label": f"GPU {idx} Leistung", "value": float(power), "unit": "W"})
    out.append({"id": f"gpu{idx}_util", "label": f"GPU {idx} Last", "value": float(util), "unit": "%"})
print(json.dumps({"source": "llm-vm", "sensors": out}))
')
curl -fsS --max-time 4 --cacert "$AQUACONTROL_CA" \
     -H "Authorization: Bearer $AQUACONTROL_TOKEN" -H "Content-Type: application/json" \
     -d "$sensors" "$AQUACONTROL_URL/api/external" >/dev/null
