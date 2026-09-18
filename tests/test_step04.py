"""Step 04 validation tests."""
import sys
sys.path.insert(0, ".")

from app.schemas import parse_request, DomainValidationError
from pydantic import ValidationError

BATTERY = {
    "capacity_kwh": 200,
    "initial_energy_kwh": 100,
    "minimum_energy_kwh": 20,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50,
}

def make_hours(count=24, start=0):
    return [
        {"hour": h, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}
        for h in range(start, start + count)
    ]

passed = 0
failed = 0

# Test 1: Valid payload
try:
    r = parse_request({
        "scenario_id": "T",
        "operator_notes": ["n"],
        "hours": make_hours(24),
        "battery": BATTERY,
    })
    assert r.scenario_id == "T" and len(r.hours) == 24
    print("PASS: Test 1 - valid payload")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 1 - {type(e).__name__}: {e}")
    failed += 1

# Test 2: 23 hours -> structural ValidationError
try:
    parse_request({
        "scenario_id": "T",
        "operator_notes": ["n"],
        "hours": make_hours(23),
        "battery": BATTERY,
    })
    print("FAIL: Test 2 - should have raised")
    failed += 1
except ValidationError:
    print("PASS: Test 2 - 23 hours -> structural ValidationError")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 2 - wrong exception: {type(e).__name__}: {e}")
    failed += 1

# Test 3: Duplicate hours (hour 5 twice, hour 23 missing) -> DomainValidationError
try:
    dup_hours = [
        {"hour": (h if h != 23 else 5), "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 5}
        for h in range(24)
    ]
    parse_request({
        "scenario_id": "T",
        "operator_notes": ["n"],
        "hours": dup_hours,
        "battery": BATTERY,
    })
    print("FAIL: Test 3 - should have raised")
    failed += 1
except DomainValidationError:
    print("PASS: Test 3 - dup hours -> DomainValidationError")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 3 - wrong exception: {type(e).__name__}: {e}")
    failed += 1

# Test 4: Empty notes -> rejected
try:
    parse_request({
        "scenario_id": "T",
        "operator_notes": [],
        "hours": make_hours(24),
        "battery": BATTERY,
    })
    print("FAIL: Test 4 - should have raised")
    failed += 1
except (ValidationError, DomainValidationError):
    print("PASS: Test 4 - empty notes -> rejected")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 4 - wrong exception: {type(e).__name__}: {e}")
    failed += 1

# Test 5: Negative tariff accepted
try:
    neg_hours = [
        {"hour": h, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": -2}
        for h in range(24)
    ]
    r = parse_request({
        "scenario_id": "T",
        "operator_notes": ["n"],
        "hours": neg_hours,
        "battery": BATTERY,
    })
    assert r.hours[0].tariff_bdt_per_kwh == -2.0
    print("PASS: Test 5 - negative tariff accepted")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 5 - {type(e).__name__}: {e}")
    failed += 1

# Test 6: to_scenario produces dense arrays
try:
    from app.schemas import to_scenario
    r = parse_request({
        "scenario_id": "S1",
        "operator_notes": ["note1"],
        "hours": make_hours(24),
        "battery": BATTERY,
    })
    sc = to_scenario(r)
    assert len(sc.demand) == 24 and len(sc.solar) == 24 and len(sc.tariff) == 24
    assert sc.capacity_kwh == 200 and sc.initial_energy_kwh == 100
    print("PASS: Test 6 - to_scenario produces dense Scenario")
    passed += 1
except Exception as e:
    print(f"FAIL: Test 6 - {type(e).__name__}: {e}")
    failed += 1

print(f"\n{passed}/{passed+failed} tests passed")
if failed:
    sys.exit(1)
