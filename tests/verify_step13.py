"""Step 13 verification: end-to-end pipeline test."""
import asyncio
import json
import sys

from app.schemas import parse_request
from app.pipeline import run

async def test():
    with open("local_request.json") as f:
        body = json.load(f)

    req = parse_request(body)
    result = await run(req)

    errors = []

    # 1. scenario_id echoed
    if result["scenario_id"] != "LOCAL-001":
        errors.append(f"scenario_id: {result['scenario_id']}")
    print(f"[{'PASS' if result['scenario_id'] == 'LOCAL-001' else 'FAIL'}] scenario_id")

    # 2. 24 entries, hours 0..23
    plan = result["hourly_plan"]
    hours = [e["hour"] for e in plan]
    ok = len(plan) == 24 and hours == list(range(24))
    if not ok:
        errors.append(f"plan len/hours: {len(plan)}")
    print(f"[{'PASS' if ok else 'FAIL'}] hourly_plan 24 entries")

    # 3. directive_interpretation
    di = result["directive_interpretation"]
    di_ok = (len(di) == 1 and di[0]["note_index"] == 0 and
             di[0]["applies"] is False and di[0]["directive_type"] == "no_op" and
             di[0]["structured_adjustment"] is None)
    if not di_ok:
        errors.append(f"directive_interpretation wrong")
    print(f"[{'PASS' if di_ok else 'FAIL'}] directive_interpretation")

    # 4. Battery neutrality
    e23 = plan[23]["battery_energy_after_kwh"]
    neut_ok = e23 == 100.0
    if not neut_ok:
        errors.append(f"E[23]={e23}")
    print(f"[{'PASS' if neut_ok else 'FAIL'}] battery neutrality E[23]={e23}")

    # 5. Totals consistent
    grid_sum = sum(e["grid_kwh"] for e in plan)
    grid_ok = abs(grid_sum - result["total_grid_kwh"]) < 0.01
    peak = max(e["grid_kwh"] for e in plan)
    peak_ok = abs(peak - result["peak_grid_kwh"]) < 0.01
    if not grid_ok:
        errors.append(f"total_grid mismatch")
    if not peak_ok:
        errors.append(f"peak_grid mismatch")
    print(f"[{'PASS' if grid_ok and peak_ok else 'FAIL'}] totals consistent")

    # 6. All 7 top-level fields present
    required = ["scenario_id", "directive_interpretation", "hourly_plan",
                 "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"]
    fields_ok = all(k in result for k in required)
    print(f"[{'PASS' if fields_ok else 'FAIL'}] all 7 fields present")

    # 7. plan_summary is a string
    summary_ok = isinstance(result["plan_summary"], str) and len(result["plan_summary"]) > 0
    print(f"[{'PASS' if summary_ok else 'FAIL'}] plan_summary")

    if errors:
        print(f"\nFAILED: {errors}")
        sys.exit(1)
    else:
        print("\nAll Step 13 checks PASSED")

asyncio.run(test())
