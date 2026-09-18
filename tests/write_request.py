import json
body = {
    "scenario_id": "LOCAL-001",
    "operator_notes": ["Solar output will drop to about 20% from 1 PM to 3 PM."],
    "hours": [{"hour": h, "demand_kwh": 100+h, "solar_kwh": 0, "tariff_bdt_per_kwh": 5+(h%7)} for h in range(24)],
    "battery": {"capacity_kwh": 200, "initial_energy_kwh": 100, "minimum_energy_kwh": 20, "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50}
}
with open("local_request.json", "w", encoding="utf-8") as f:
    json.dump(body, f, indent=2)
print("local_request.json written")
