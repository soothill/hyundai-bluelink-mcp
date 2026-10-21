# Hyundai Bluelink MCP Server

MCP (Model Context Protocol) server exposing the full [hyundai-kia-connect-api](https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api) feature set: monitor and remotely control a Hyundai/Kia EV (Ioniq 5/6/9, EV6, EV9, …) through the Bluelink cloud.

Ships as a Docker container speaking **streamable-http** (default, port 8080) or **stdio**.

## Tools (27)

| Area | Tools |
|---|---|
| Status | `bluelink_list_vehicles`, `bluelink_status` (cached, cheap), `bluelink_force_refresh` (wakes car), `bluelink_check_action_status` |
| Charging | `bluelink_start_charge`, `bluelink_stop_charge`, `bluelink_set_charge_limits` (AC/DC %), `bluelink_set_charging_current`, `bluelink_open_charge_port`, `bluelink_close_charge_port`, `bluelink_set_schedule` (departures, off-peak, climate), `bluelink_set_v2l_discharge_limit` |
| Climate | `bluelink_start_climate`, `bluelink_stop_climate` |
| Security/body | `bluelink_lock`, `bluelink_unlock`, `bluelink_start_hazard_lights`, `bluelink_hazard_lights_and_horn`, `bluelink_set_windows`, `bluelink_valet_mode` |
| Misc | `bluelink_set_navigation`, `bluelink_month_trip_info`, `bluelink_day_trip_info`, `bluelink_svm_details`, `bluelink_svm_capture` |
| Auth | `bluelink_send_otp`, `bluelink_verify_otp` (only if login demands an OTP) |

`bluelink_status` returns the full parsed vehicle state: SOC, charging/plugged-in, charge power, estimated durations, charge limits, battery temps/SOH, range, doors/windows/locks, tyre pressures, odometer, schedules, and more.

## Environment

| Var | Default | Notes |
|---|---|---|
| `BLUELINK_USERNAME` | — | Bluelink account email |
| `BLUELINK_PASSWORD` | — | Bluelink account password |
| `BLUELINK_USERNAME_FILE` | — | **Preferred:** read username from this file instead of the env value |
| `BLUELINK_PASSWORD_FILE` | — | **Preferred:** read password from this file (Docker `_FILE` convention) |
| `BLUELINK_PIN_FILE` | — | Same, for the PIN |
| `BLUELINK_PIN` | `` | Required for Canada, maybe USA |

Prefer the `_FILE` variants so credentials never appear in `docker inspect`, process
env, or shell history — e.g. mount a secrets volume and set
`BLUELINK_PASSWORD_FILE=/run/secrets/bluelink_password`.
| `BLUELINK_REGION` | `1` | 1=EU 2=CA 3=US 4=CN 5=AU 6=IN 7=NZ 8=BR |
| `BLUELINK_BRAND` | `2` | 1=Kia 2=Hyundai 3=Genesis |
| `BLUELINK_MCP_TRANSPORT` | `streamable-http` | or `stdio` |
| `BLUELINK_MCP_HOST` / `BLUELINK_MCP_PORT` | `0.0.0.0` / `8080` | HTTP transport bind |

## Run with Docker

```bash
docker run -d --name bluelink-mcp -p 8080:8080 \
  -e BLUELINK_USERNAME='you@example.com' \
  -e BLUELINK_PASSWORD='secret' \
  ghcr.io/soothill/hyundai-bluelink-mcp:latest
```

MCP endpoint: `http://localhost:8080/mcp` (streamable-http).

### stdio (e.g. Claude Desktop / OpenClaw)

```bash
docker run -i --rm -e BLUELINK_USERNAME=... -e BLUELINK_PASSWORD=... \
  -e BLUELINK_MCP_TRANSPORT=stdio ghcr.io/soothill/hyundai-bluelink-mcp:latest
```

## Run without Docker

```bash
pip install -r requirements.txt
BLUELINK_USERNAME=... BLUELINK_PASSWORD=... python server.py
```

## Etiquette / rate limits

- `bluelink_status` reads the cloud cache — safe to poll (EU ≈200 req/day overall).
- `bluelink_force_refresh` and control commands **wake the car**; each wake costs the 12V battery. Keep force-refreshes to a few per day.
- Control actions return an `action_id`; poll `bluelink_check_action_status` for SUCCESS/FAILED.

## Disclaimer

Unofficial; uses reverse-engineered Hyundai/Kia cloud APIs. Not affiliated with Hyundai. If an app update changes the API, updates land in the upstream library — keep this image rebuilt periodically. MIT (library) / MIT (this wrapper).
