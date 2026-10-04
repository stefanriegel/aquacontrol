# aquacontrol

Web control for an **Aquacomputer QUADRO** fan controller on a Linux/Proxmox host, replacing the
Windows-only aquasuite for day-to-day use.

The QUADRO runs its fan curves and LED effects on its own from settings stored in the device.
aquacontrol reads live values, edits those stored settings (fan mode, target temperature, curve,
min/max, LED strip on/off and brightness), switches the LEDs on a time schedule, and shows other
temperatures (host hwmon sensors, values pushed from other machines) for reference.

- Python 3.11+ standard library only, no dependencies
- Talks to the device via Linux `hidraw` (feature report 0x03 = settings, input report 0x01 = live data)
- Every write: fresh read → CRC check → backup → write → commit → read-back verify → rollback on mismatch

> Protocol knowledge builds on [TimSC/quadroctl](https://github.com/TimSC/quadroctl) and the
> [aquacomputer_d5next hwmon driver](https://github.com/aleksamagicka/aquacomputer_d5next-hwmon).
> Tested with QUADRO firmware 1033 only. Writing settings to hardware is at your own risk.

## Install (Proxmox host, as root)

```bash
rsync -a --exclude .git ./ root@pve:/root/aquacontrol-src/
ssh root@pve /root/aquacontrol-src/deploy/install.sh
ssh root@pve 'cd /opt/aquacontrol && python3 -m aquacontrol set-password && systemctl restart aquacontrol'
```

Web UI: `https://<host>:8443/` (user name is ignored, password as set).

Before the first change, store a pinned backup of the device settings:

```bash
runuser -u aquacontrol -- sh -c 'cd /opt/aquacontrol && python3 -m aquacontrol backup --pinned --reason initial'
```

## Pushing extra sensors (e.g. GPUs from a VM)

```bash
cd /opt/aquacontrol && python3 -m aquacontrol add-push-token llm-vm   # prints the token once
systemctl restart aquacontrol
```

On the sending machine install `deploy/push-gpu/` (script, service, timer) and create
`/etc/aquacontrol-push.env` (mode 0600):

```
AQUACONTROL_URL=https://<pve-host>:8443
AQUACONTROL_TOKEN=<token>
AQUACONTROL_CA=/etc/aquacontrol-ca.pem
```

## Development

```bash
python3 -m unittest discover -s tests -t .
# UI against a simulated device:
python3 -m aquacontrol --daemon-config dev/daemon.json --app-config dev/config.json \
    run --fake tests/fixtures --no-tls --listen 127.0.0.1 --port 18443
```

`dev/daemon.json` needs a `password_hash` (create one with
`python3 -c 'from aquacontrol.auth import hash_password; print(hash_password("devpassword1"))'`).

Test fixtures are real device reports. Status reports must have the device serial zeroed
(`tools/make_fixture.py` does that); the settings and names reports contain no serial.
