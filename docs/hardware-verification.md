# Hardware verification

These tests were run on a real Aquacomputer QUADRO with **firmware 1033**, connected by USB to a Proxmox VE 9 host
(kernel 6.17, Debian 13, Python 3.13), on 2026-10-04.

| Area | Result |
| --- | --- |
| Read path | Live values via hidraw match the kernel `aquacomputer_d5next` hwmon driver exactly: temperature, all fan RPMs, flow. The settings decode matches the device, and a feature read returns 961 bytes. |
| No-op write (`selftest-write`) | The settings were written, committed as **feature** report 0x02 (as the kernel driver does) and read back byte-identical. There are no device-volatile bytes. |
| Real fan write | Channel 4 set to fixed 60 % went from 499 to 1094 rpm. Restoring the backup returned it to target-temperature mode at about 540 rpm. The known firmware-1033 "write has no effect" issue does not occur. |
| Persistence | Target temperature changed, then the host was shut down so the QUADRO lost power. The device power-up counter went from 220 to 221, and the setting was kept. |
| LED strip | Brightness 60, strip off (flag `0x0002`) and on again were confirmed visually. |
| Schedule | Test rules switched the strip off and on at the configured minute. |
| Curve semantics | The device maps curve percentages linearly into [min, max]. Pump example: curve 4.31 % with min 28.02 / max 90.3 gives an output of 30.70 %, and the device reported 30.7 %. |
| LED layout | Decoded by driving aquasuite and diffing captured USB writes, see [led-layout.md](led-layout.md). The editor reproduces aquasuite's writes byte for byte. |
| systemd hardening | `systemd-analyze security aquacontrol` scores 1.6 ("OK"). Read, fan write and rollback, LED write, schedule, sensor push and clean stop all work under the hardened unit. |
| History persistence | 7 buckets before a service restart, 8 after, with the same first timestamp. |
