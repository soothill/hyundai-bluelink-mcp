"""Hyundai Bluelink MCP server.

Exposes the full hyundai-kia-connect-api feature set as MCP tools so an MCP
client can monitor and remotely act on a Hyundai/Kia EV (charge control,
climate, locks, windows, schedules, SVM, trips, navigation).

Credentials via environment:
  BLUELINK_USERNAME / BLUELINK_PASSWORD  (required)
  BLUELINK_PIN       (default ""; required for CA, maybe USA)
  BLUELINK_REGION    (default 1 = EU)   BLUELINK_BRAND (default 2 = Hyundai)
Transport:
  BLUELINK_MCP_TRANSPORT = "streamable-http" (default) | "stdio"
  BLUELINK_MCP_HOST (default 0.0.0.0)  BLUELINK_MCP_PORT (default 8080)
"""

import dataclasses
import datetime as dt
import enum
import json
import logging
import os
import threading

import anyio

from mcp.server.mcpserver import MCPServer

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.ApiImpl import (
    ClimateRequestOptions,
    POICoord,
    POIInfo,
    ScheduleChargingClimateRequestOptions,
    WindowRequestOptions,
)
from hyundai_kia_connect_api.const import OTP_NOTIFY_TYPE, WINDOW_STATE

log = logging.getLogger("bluelink-mcp")
logging.basicConfig(level=logging.INFO)

mcp = MCPServer(
    name="hyundai-bluelink",
    title="Hyundai Bluelink",
    description="Monitor and control a Hyundai/Kia EV via the Bluelink cloud API.",
    instructions=(
        "Read battery/charging state with bluelink_status. Remote actions wake "
        "the car; avoid hammering bluelink_force_refresh (each call wakes the "
        "vehicle and drains the 12V battery). Actions return an action_id; "
        "poll bluelink_check_action_status until SUCCESS/FAILED."
    ),
)

_vm: VehicleManager | None = None
_vm_lock = threading.Lock()
_otp_state: dict = {}


# ----------------------------------------------------------------- plumbing

def _get_vm() -> VehicleManager:
    global _vm
    with _vm_lock:
        if _vm is None:
            vm = VehicleManager(
                region=int(os.environ.get("BLUELINK_REGION", "1")),
                brand=int(os.environ.get("BLUELINK_BRAND", "2")),
                username=os.environ["BLUELINK_USERNAME"],
                password=os.environ["BLUELINK_PASSWORD"],
                pin=os.environ.get("BLUELINK_PIN", ""),
            )
            result = vm.login()
            if isinstance(result, bool) and result:
                _vm = vm
            else:
                # OTP flow: caller must use bluelink_send_otp / verify tools.
                _otp_state["manager"] = vm
                raise RuntimeError(
                    "Bluelink login requires an OTP. Call bluelink_send_otp, "
                    "then bluelink_verify_otp with the code."
                )
        _vm.check_and_refresh_token()
        return _vm


def _vehicle_id(vm: VehicleManager, vehicle_id: str | None) -> str:
    if vehicle_id:
        return vehicle_id
    ids = list(vm.vehicles.keys())
    if not ids:
        raise RuntimeError("No vehicles on this account")
    return ids[0]


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, (dt.datetime, dt.time, dt.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return str(value)


def _serialize_vehicle(vehicle) -> dict:
    out = {}
    for name in dir(vehicle):
        if name.startswith("_"):
            continue
        try:
            value = getattr(vehicle, name)
        except Exception:  # noqa: BLE001 - property may raise on missing data
            continue
        if callable(value):
            continue
        out[name] = _jsonable(value)
    return out


def _run(fn, *args, **kwargs):
    return anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


def _time_or_none(s):
    if not s:
        return None
    return dt.time.fromisoformat(s)


# --------------------------------------------------------------- status

@mcp.tool(description="List vehicles on the Bluelink account (id, name, model).")
async def bluelink_list_vehicles() -> str:
    vm = await _run(_get_vm)
    await _run(vm.update_all_vehicles_with_cached_state)
    return json.dumps(
        [
            {
                "vehicle_id": vid,
                "name": v.name,
                "model": v.model,
                "year": v.year,
                "ev_battery_percentage": v.ev_battery_percentage,
                "is_charging": v.ev_battery_is_charging,
                "plugged_in": v.ev_battery_is_plugged_in,
            }
            for vid, v in vm.vehicles.items()
        ],
        indent=2,
    )


@mcp.tool(
    description=(
        "Full vehicle status from the Bluelink cloud cache (SOC, charging "
        "state, power, durations, limits, doors, climate, temps, odometer...). "
        "Cheap; does not wake the car."
    )
)
async def bluelink_status(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    await _run(vm.update_vehicle_with_cached_state, vid)
    return json.dumps(_serialize_vehicle(vm.get_vehicle(vid)), indent=2)


@mcp.tool(
    description=(
        "Wake the car and fetch live data (use sparingly; drains 12V battery "
        "and is rate-limited). Then read bluelink_status."
    )
)
async def bluelink_force_refresh(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    await _run(vm.force_refresh_vehicle_state, vid)
    await _run(vm.update_vehicle_with_cached_state, vid)
    return json.dumps(_serialize_vehicle(vm.get_vehicle(vid)), indent=2)


@mcp.tool(description="Poll the status of a previously issued action_id.")
async def bluelink_check_action_status(
    action_id: str, vehicle_id: str | None = None, synchronous: bool = True, timeout: int = 60
) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    status = await _run(vm.check_action_status, vid, action_id, synchronous, timeout)
    return json.dumps({"action_id": action_id, "status": _jsonable(status)})


# --------------------------------------------------------------- charging

@mcp.tool(description="Start charging the vehicle (must be plugged in).")
async def bluelink_start_charge(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.start_charge, vid)
    return json.dumps({"action_id": aid})


@mcp.tool(description="Stop charging the vehicle.")
async def bluelink_stop_charge(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.stop_charge, vid)
    return json.dumps({"action_id": aid})


@mcp.tool(description="Set target charge limits in % (AC and DC separately).")
async def bluelink_set_charge_limits(ac: int, dc: int, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.set_charge_limits, vid, int(ac), int(dc))
    return json.dumps({"action_id": aid, "ac": ac, "dc": dc})


@mcp.tool(description="Set max charging current level percent (e.g. 100, 90, 80, 60).")
async def bluelink_set_charging_current(level: int, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.set_charging_current, vid, int(level))
    return json.dumps({"action_id": aid, "level": level})


@mcp.tool(description="Open the electric charge port door.")
async def bluelink_open_charge_port(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps({"action_id": await _run(vm.open_charge_port, vid)})


@mcp.tool(description="Close the electric charge port door.")
async def bluelink_close_charge_port(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps({"action_id": await _run(vm.close_charge_port, vid)})


@mcp.tool(
    description=(
        "Set the car's charge/climate schedule. All fields optional; None = "
        "leave unchanged. first/second_departure: {enabled, days:[0-6 Sun=0], "
        "time:'HH:MM'}. off_peak times 'HH:MM'. temperature in C."
    )
)
async def bluelink_set_schedule(
    vehicle_id: str | None = None,
    first_departure: dict | None = None,
    second_departure: dict | None = None,
    charging_enabled: bool | None = None,
    off_peak_start_time: str | None = None,
    off_peak_end_time: str | None = None,
    off_peak_charge_only_enabled: bool | None = None,
    climate_enabled: bool | None = None,
    temperature: float | None = None,
    defrost: bool | None = None,
) -> str:
    def _dep(d):
        if d is None:
            return None
        return ScheduleChargingClimateRequestOptions.DepartureOptions(
            enabled=d.get("enabled"),
            days=d.get("days"),
            time=_time_or_none(d.get("time")),
        )

    opts = ScheduleChargingClimateRequestOptions(
        first_departure=_dep(first_departure),
        second_departure=_dep(second_departure),
        charging_enabled=charging_enabled,
        off_peak_start_time=_time_or_none(off_peak_start_time),
        off_peak_end_time=_time_or_none(off_peak_end_time),
        off_peak_charge_only_enabled=off_peak_charge_only_enabled,
        climate_enabled=climate_enabled,
        temperature=temperature,
        defrost=defrost,
    )
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.schedule_charging_and_climate, vid, opts)
    return json.dumps({"action_id": aid})


@mcp.tool(description="Set V2L/V2X discharge limit percent (where supported).")
async def bluelink_set_v2l_discharge_limit(limit: int, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.set_vehicle_to_load_discharge_limit, vid, int(limit))
    return json.dumps({"action_id": aid, "limit": limit})


# --------------------------------------------------------------- climate

@mcp.tool(
    description=(
        "Start remote climate (HVAC). set_temp in C, duration minutes, "
        "seat heaters 0-3, heating 0/1, climate 0/1, defrost bool, "
        "steering_wheel 0/1, side_rear_mirror_heating 0/1."
    )
)
async def bluelink_start_climate(
    set_temp: float | None = None,
    duration: int | None = None,
    defrost: bool | None = None,
    climate: bool | None = None,
    heating: int | None = None,
    front_left_seat: int | None = None,
    front_right_seat: int | None = None,
    rear_left_seat: int | None = None,
    rear_right_seat: int | None = None,
    steering_wheel: int | None = None,
    side_rear_mirror_heating: int | None = None,
    vehicle_id: str | None = None,
) -> str:
    opts = ClimateRequestOptions(
        set_temp=set_temp,
        duration=duration,
        defrost=defrost,
        climate=climate,
        heating=heating,
        front_left_seat=front_left_seat,
        front_right_seat=front_right_seat,
        rear_left_seat=rear_left_seat,
        rear_right_seat=rear_right_seat,
        steering_wheel=steering_wheel,
        side_rear_mirror_heating=side_rear_mirror_heating,
    )
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.start_climate, vid, opts)
    return json.dumps({"action_id": aid})


@mcp.tool(description="Stop remote climate.")
async def bluelink_stop_climate(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps({"action_id": await _run(vm.stop_climate, vid)})


# --------------------------------------------------------------- security / body

@mcp.tool(description="Lock the doors.")
async def bluelink_lock(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    return json.dumps({"action_id": await _run(vm.lock, _vehicle_id(vm, vehicle_id))})


@mcp.tool(description="Unlock the doors.")
async def bluelink_unlock(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    return json.dumps({"action_id": await _run(vm.unlock, _vehicle_id(vm, vehicle_id))})


@mcp.tool(description="Flash hazard lights.")
async def bluelink_start_hazard_lights(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    return json.dumps({"action_id": await _run(vm.start_hazard_lights, _vehicle_id(vm, vehicle_id))})


@mcp.tool(description="Hazard lights + horn (find-my-car).")
async def bluelink_hazard_lights_and_horn(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    return json.dumps(
        {"action_id": await _run(vm.start_hazard_lights_and_horn, _vehicle_id(vm, vehicle_id))}
    )


@mcp.tool(description="Open/close/vent windows. state: 0=closed 1=open 2=vent, per position.")
async def bluelink_set_windows(
    front_left: int | None = None,
    front_right: int | None = None,
    back_left: int | None = None,
    back_right: int | None = None,
    vehicle_id: str | None = None,
) -> str:
    opts = WindowRequestOptions(
        front_left=WINDOW_STATE(front_left) if front_left is not None else None,
        front_right=WINDOW_STATE(front_right) if front_right is not None else None,
        back_left=WINDOW_STATE(back_left) if back_left is not None else None,
        back_right=WINDOW_STATE(back_right) if back_right is not None else None,
    )
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps({"action_id": await _run(vm.set_windows_state, vid, opts)})


@mcp.tool(description="Activate or deactivate valet mode (where supported).")
async def bluelink_valet_mode(activate: bool, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    aid = await _run(vm.start_valet_mode if activate else vm.stop_valet_mode, vid)
    return json.dumps({"action_id": aid})


# --------------------------------------------------------------- misc

@mcp.tool(description="Send a navigation destination (lat/lon + name) to the car's nav.")
async def bluelink_set_navigation(
    latitude: float, longitude: float, name: str = "", vehicle_id: str | None = None
) -> str:
    poi = POIInfo(coord=POICoord(lat=float(latitude), lon=float(longitude)), name=name)
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps({"action_id": await _run(vm.set_navigation, vid, [poi])})


@mcp.tool(description="Monthly trip/energy stats for 'YYYY-MM'.")
async def bluelink_month_trip_info(yyyymm: str, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    await _run(vm.update_month_trip_info, vid, yyyymm)
    return json.dumps(_jsonable(vm.get_vehicle(vid).month_trip_info), indent=2)


@mcp.tool(description="Daily trip stats for 'YYYY-MM-DD'.")
async def bluelink_day_trip_info(yyyymmdd: str, vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    await _run(vm.update_day_trip_info, vid, yyyymmdd)
    return json.dumps(_jsonable(vm.get_vehicle(vid).day_trip_info), indent=2)


@mcp.tool(description="Latest surround-view (SVM) camera capture metadata.")
async def bluelink_svm_details(vehicle_id: str | None = None) -> str:
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps(_jsonable(await _run(vm.get_svm_details, vid)), indent=2)


@mcp.tool(
    description=(
        "Trigger a fresh SVM 360 capture (wakes the car). Requires "
        "acknowledged_warning=true: the capture can wake the vehicle."
    )
)
async def bluelink_svm_capture(
    acknowledged_warning: bool, vehicle_id: str | None = None
) -> str:
    if not acknowledged_warning:
        raise RuntimeError("Pass acknowledged_warning=true to wake the vehicle for a capture.")
    vm = await _run(_get_vm)
    vid = _vehicle_id(vm, vehicle_id)
    return json.dumps(_jsonable(await _run(vm.request_svm_capture, vid, True)), indent=2)


# --------------------------------------------------------------- OTP (rare)

@mcp.tool(description="Send an OTP to email or sms when Bluelink login demands one.")
async def bluelink_send_otp(method: str = "email") -> str:
    vm = _otp_state.get("manager") or _vm
    if vm is None:
        raise RuntimeError("Login not started yet - call any tool first to trigger login.")
    notify = OTP_NOTIFY_TYPE.EMAIL if method.lower() == "email" else OTP_NOTIFY_TYPE.SMS
    await _run(vm.send_otp, notify)
    return json.dumps({"sent_to": method})


@mcp.tool(description="Complete OTP login with the received code.")
async def bluelink_verify_otp(otp_code: str) -> str:
    global _vm
    vm = _otp_state.get("manager")
    if vm is None:
        raise RuntimeError("No OTP login in progress.")
    await _run(vm.verify_otp_and_complete_login, otp_code)
    _vm = vm
    _otp_state.clear()
    return json.dumps({"logged_in": True})


def main() -> None:
    transport = os.environ.get("BLUELINK_MCP_TRANSPORT", "streamable-http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("BLUELINK_MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("BLUELINK_MCP_PORT", "8080")),
        )


if __name__ == "__main__":
    main()
