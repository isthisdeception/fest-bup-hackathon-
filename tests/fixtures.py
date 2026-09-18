"""
Locally authored test fixtures for the optimizer.

These are synthetic scenarios — NOT from the public sample pack.
They exist to verify LP correctness without spending LLM calls or
depending on external data.
"""

from typing import Any, Callable

from app.schemas import Scenario
from app.directives import Directive, compile_constraints


def flat_tariff_no_solar() -> tuple[Scenario, list[Directive]]:
    """Flat tariff, no solar, no directives.

    Expected: cost = sum(demand) * tariff, battery idle.
    """
    sc = Scenario(
        scenario_id="FIXTURE-FLAT",
        operator_notes=["no directives"],
        demand=[100.0] * 24,
        solar=[0.0] * 24,
        tariff=[10.0] * 24,
        capacity_kwh=200.0,
        initial_energy_kwh=100.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    return sc, []


def two_tariff_arbitrage() -> tuple[Scenario, list[Directive]]:
    """Two-tariff: cheap hours 0-5 at 5 BDT, expensive 18-21 at 20 BDT.

    Expected: charges in cheap hours, discharges in expensive hours.
    """
    tariff = [10.0] * 24
    for h in range(0, 6):
        tariff[h] = 5.0
    for h in range(18, 22):
        tariff[h] = 20.0

    sc = Scenario(
        scenario_id="FIXTURE-ARB",
        operator_notes=["no directives"],
        demand=[100.0] * 24,
        solar=[0.0] * 24,
        tariff=tariff,
        capacity_kwh=200.0,
        initial_energy_kwh=100.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    return sc, []


def no_charge_window_cheap() -> tuple[Scenario, list[Directive]]:
    """No-charge window over the cheap hours 0-5."""
    sc, _ = two_tariff_arbitrage()
    directives = [
        Directive(
            note_index=0,
            directive_type="no_charge_window",
            hours=tuple(range(0, 6)),
            explanation="No charging during cheap hours",
            source="test_injected",
        ),
    ]
    return sc, directives


def no_discharge_window_expensive() -> tuple[Scenario, list[Directive]]:
    """No-discharge window over the expensive hours 18-21."""
    sc, _ = two_tariff_arbitrage()
    directives = [
        Directive(
            note_index=0,
            directive_type="no_discharge_window",
            hours=tuple(range(18, 22)),
            explanation="No discharging during expensive hours",
            source="test_injected",
        ),
    ]
    return sc, directives


def max_grid_cap() -> tuple[Scenario, list[Directive]]:
    """Max grid cap of 80 kWh on hours 10-14."""
    sc, _ = flat_tariff_no_solar()
    directives = [
        Directive(
            note_index=0,
            directive_type="max_grid_window",
            hours=tuple(range(10, 15)),
            max_grid_kwh=80.0,
            explanation="Grid cap",
            source="test_injected",
        ),
    ]
    return sc, directives


def min_reserve() -> tuple[Scenario, list[Directive]]:
    """Minimum battery reserve of 80 kWh on hours 10-14.

    E0=100, so battery cannot discharge below 80 kWh during those hours.
    """
    sc, _ = two_tariff_arbitrage()
    directives = [
        Directive(
            note_index=0,
            directive_type="minimum_battery_reserve",
            hours=tuple(range(10, 15)),
            minimum_energy_kwh=80.0,
            explanation="Reserve requirement",
            source="test_injected",
        ),
    ]
    return sc, directives


def solar_reduction_scenario() -> tuple[Scenario, list[Directive]]:
    """Solar reduction factor 0.2 on hours 8-14."""
    sc = Scenario(
        scenario_id="FIXTURE-SOLAR",
        operator_notes=["solar reduction"],
        demand=[100.0] * 24,
        solar=[50.0] * 24,
        tariff=[10.0] * 24,
        capacity_kwh=200.0,
        initial_energy_kwh=100.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    directives = [
        Directive(
            note_index=0,
            directive_type="solar_reduction",
            hours=tuple(range(8, 15)),
            factor=0.2,
            explanation="Solar reduced to 20%",
            source="test_injected",
        ),
    ]
    return sc, directives


def negative_tariff() -> tuple[Scenario, list[Directive]]:
    """Negative tariff in one hour (hour 12 at -5 BDT)."""
    tariff = [10.0] * 24
    tariff[12] = -5.0

    sc = Scenario(
        scenario_id="FIXTURE-NEGTARIFF",
        operator_notes=["no directives"],
        demand=[100.0] * 24,
        solar=[0.0] * 24,
        tariff=tariff,
        capacity_kwh=200.0,
        initial_energy_kwh=100.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    return sc, []


def infeasible_directives() -> tuple[Scenario, list[Directive]]:
    """Intentionally infeasible: reserve=capacity for all hours + no-charge all hours.

    E0=50 < capacity=200, so the battery can never reach 200 kWh reserve
    if charging is blocked.
    """
    sc = Scenario(
        scenario_id="FIXTURE-INFEASIBLE",
        operator_notes=["reserve", "no charge"],
        demand=[100.0] * 24,
        solar=[0.0] * 24,
        tariff=[10.0] * 24,
        capacity_kwh=200.0,
        initial_energy_kwh=50.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    directives = [
        Directive(
            note_index=0,
            directive_type="minimum_battery_reserve",
            hours=tuple(range(24)),
            minimum_energy_kwh=200.0,
            explanation="Reserve at full capacity",
            source="test_injected",
        ),
        Directive(
            note_index=1,
            directive_type="no_charge_window",
            hours=tuple(range(24)),
            explanation="No charging ever",
            source="test_injected",
        ),
    ]
    return sc, directives


# ─── 19 Scenario Stress Fixtures (Step 20) ──────────────────────────────────

def _base_scenario(scenario_id: str, **kwargs) -> Scenario:
    """Helper for constructing stress scenarios with sensible defaults."""
    defaults = {
        "scenario_id": scenario_id,
        "operator_notes": ["stress fixture"],
        "demand": [100.0] * 24,
        "solar": [30.0 if 8 <= h < 16 else 0.0 for h in range(24)],
        "tariff": [5.0 if 0 <= h < 6 else (20.0 if 18 <= h < 22 else 10.0) for h in range(24)],
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 100.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    }
    defaults.update(kwargs)
    return Scenario(**defaults)


def stress_initial_energy_at_min_energy() -> tuple[Scenario, list[Directive]]:
    """1. Battery initial_energy_kwh == minimum_energy_kwh (no discharge headroom at start)."""
    sc = _base_scenario("STRESS-01", initial_energy_kwh=20.0, minimum_energy_kwh=20.0)
    return sc, []


def stress_initial_energy_at_capacity() -> tuple[Scenario, list[Directive]]:
    """2. Battery initial_energy_kwh == capacity_kwh (no charge headroom at start)."""
    sc = _base_scenario("STRESS-02", initial_energy_kwh=200.0, capacity_kwh=200.0)
    return sc, []


def stress_minimum_energy_zero() -> tuple[Scenario, list[Directive]]:
    """3. minimum_energy_kwh == 0."""
    sc = _base_scenario("STRESS-03", minimum_energy_kwh=0.0)
    return sc, []


def stress_max_charge_zero() -> tuple[Scenario, list[Directive]]:
    """4. max_charge_kwh_per_hour == 0 (charging impossible - neutrality forces all-idle)."""
    sc = _base_scenario("STRESS-04", max_charge_kwh_per_hour=0.0)
    return sc, []


def stress_max_discharge_zero() -> tuple[Scenario, list[Directive]]:
    """5. max_discharge_kwh_per_hour == 0 (discharging impossible - neutrality forces all-idle)."""
    sc = _base_scenario("STRESS-05", max_discharge_kwh_per_hour=0.0)
    return sc, []


def stress_all_zero_solar() -> tuple[Scenario, list[Directive]]:
    """6. All-zero solar for 24 hours."""
    sc = _base_scenario("STRESS-06", solar=[0.0] * 24)
    return sc, []


def stress_heavy_solar_curtailment() -> tuple[Scenario, list[Directive]]:
    """7. Very heavy solar (solar_kwh > demand_kwh in 8 hours - forces curtailment)."""
    solar = [300.0 if 8 <= h < 16 else 0.0 for h in range(24)]
    sc = _base_scenario("STRESS-07", solar=solar, demand=[100.0] * 24)
    return sc, []


def stress_flat_tariff() -> tuple[Scenario, list[Directive]]:
    """8. Flat tariff (no price arbitrage value)."""
    sc = _base_scenario("STRESS-08", tariff=[10.0] * 24)
    return sc, []


def stress_extreme_tariff_spike() -> tuple[Scenario, list[Directive]]:
    """9. Extreme evening tariff spike (5 BDT baseline, 45 BDT at hours 18-21)."""
    tariff = [45.0 if 18 <= h < 22 else 5.0 for h in range(24)]
    sc = _base_scenario("STRESS-09", tariff=tariff)
    return sc, []


def stress_zero_demand_hour() -> tuple[Scenario, list[Directive]]:
    """10. One hour with demand_kwh == 0."""
    demand = [100.0] * 24
    demand[3] = 0.0
    sc = _base_scenario("STRESS-10", demand=demand)
    return sc, []


def stress_zero_tariff_hours() -> tuple[Scenario, list[Directive]]:
    """11. tariff_bdt_per_kwh == 0 in several hours."""
    tariff = [10.0] * 24
    for h in range(1, 5):
        tariff[h] = 0.0
    sc = _base_scenario("STRESS-11", tariff=tariff)
    return sc, []


def stress_negative_tariff() -> tuple[Scenario, list[Directive]]:
    """12. Negative tariff in one hour (-5 BDT at hour 12)."""
    tariff = [10.0] * 24
    tariff[12] = -5.0
    sc = _base_scenario("STRESS-12", tariff=tariff)
    return sc, []


def stress_very_large_values() -> tuple[Scenario, list[Directive]]:
    """13. Very large values (demand ~ 1e5, capacity ~ 1e6)."""
    sc = Scenario(
        scenario_id="STRESS-13",
        operator_notes=["large values"],
        demand=[100_000.0] * 24,
        solar=[50_000.0 if 8 <= h < 16 else 0.0 for h in range(24)],
        tariff=[10.0] * 24,
        capacity_kwh=1_000_000.0,
        initial_energy_kwh=500_000.0,
        minimum_energy_kwh=100_000.0,
        max_charge_kwh_per_hour=250_000.0,
        max_discharge_kwh_per_hour=250_000.0,
    )
    return sc, []


def stress_very_small_values() -> tuple[Scenario, list[Directive]]:
    """14. Very small values (demand ~ 0.01, capacity ~ 0.1)."""
    sc = Scenario(
        scenario_id="STRESS-14",
        operator_notes=["small values"],
        demand=[0.01] * 24,
        solar=[0.005 if 8 <= h < 16 else 0.0 for h in range(24)],
        tariff=[10.0] * 24,
        capacity_kwh=0.1,
        initial_energy_kwh=0.05,
        minimum_energy_kwh=0.01,
        max_charge_kwh_per_hour=0.02,
        max_discharge_kwh_per_hour=0.02,
    )
    return sc, []


def stress_tight_grid_cap() -> tuple[Scenario, list[Directive]]:
    """15. max_grid_window cap tight but satisfiable."""
    # Demand is 100, solar is 0. Battery can discharge 50. Cap grid at 50 in hours 18-20.
    sc = _base_scenario("STRESS-15", solar=[0.0] * 24, initial_energy_kwh=150.0)
    directives = [
        Directive(
            note_index=0,
            directive_type="max_grid_window",
            hours=(18, 19, 20),
            max_grid_kwh=50.0,
            explanation="Tight grid cap",
            source="test_injected",
        ),
    ]
    return sc, directives


def stress_reserve_at_initial_energy() -> tuple[Scenario, list[Directive]]:
    """16. minimum_battery_reserve exactly equal to initial_energy_kwh."""
    sc = _base_scenario("STRESS-16", initial_energy_kwh=100.0)
    directives = [
        Directive(
            note_index=0,
            directive_type="minimum_battery_reserve",
            hours=(10, 11, 12),
            minimum_energy_kwh=100.0,
            explanation="Reserve equals initial energy",
            source="test_injected",
        ),
    ]
    return sc, directives


def stress_reserve_at_capacity_one_hour() -> tuple[Scenario, list[Directive]]:
    """17. minimum_battery_reserve exactly equal to capacity_kwh for one hour."""
    # Initial 100, capacity 200, max_charge 50. Battery charges at hours 11 and 12 to hit 200 at hour 13.
    sc = _base_scenario("STRESS-17", initial_energy_kwh=100.0, capacity_kwh=200.0, max_charge_kwh_per_hour=50.0)
    directives = [
        Directive(
            note_index=0,
            directive_type="minimum_battery_reserve",
            hours=(13,),
            minimum_energy_kwh=200.0,
            explanation="Reserve at full capacity for one hour",
            source="test_injected",
        ),
    ]
    return sc, directives


def stress_no_charge_24h() -> tuple[Scenario, list[Directive]]:
    """18. no_charge_window covering all 24 hours (with neutrality, forces all-idle)."""
    sc = _base_scenario("STRESS-18")
    directives = [
        Directive(
            note_index=0,
            directive_type="no_charge_window",
            hours=tuple(range(24)),
            explanation="No charge all day",
            source="test_injected",
        ),
    ]
    return sc, directives


def stress_no_charge_and_no_discharge_24h() -> tuple[Scenario, list[Directive]]:
    """19. no_charge_window and no_discharge_window covering all 24 hours simultaneously."""
    sc = _base_scenario("STRESS-19")
    directives = [
        Directive(
            note_index=0,
            directive_type="no_charge_window",
            hours=tuple(range(24)),
            explanation="No charge all day",
            source="test_injected",
        ),
        Directive(
            note_index=1,
            directive_type="no_discharge_window",
            hours=tuple(range(24)),
            explanation="No discharge all day",
            source="test_injected",
        ),
    ]
    return sc, directives


STRESS_FIXTURES: list[tuple[str, Any]] = [
    ("initial_energy_at_min_energy", stress_initial_energy_at_min_energy),
    ("initial_energy_at_capacity", stress_initial_energy_at_capacity),
    ("minimum_energy_zero", stress_minimum_energy_zero),
    ("max_charge_zero", stress_max_charge_zero),
    ("max_discharge_zero", stress_max_discharge_zero),
    ("all_zero_solar", stress_all_zero_solar),
    ("heavy_solar_curtailment", stress_heavy_solar_curtailment),
    ("flat_tariff", stress_flat_tariff),
    ("extreme_tariff_spike", stress_extreme_tariff_spike),
    ("zero_demand_hour", stress_zero_demand_hour),
    ("zero_tariff_hours", stress_zero_tariff_hours),
    ("negative_tariff", stress_negative_tariff),
    ("very_large_values", stress_very_large_values),
    ("very_small_values", stress_very_small_values),
    ("tight_grid_cap", stress_tight_grid_cap),
    ("reserve_at_initial_energy", stress_reserve_at_initial_energy),
    ("reserve_at_capacity_one_hour", stress_reserve_at_capacity_one_hour),
    ("no_charge_24h", stress_no_charge_24h),
    ("no_charge_and_no_discharge_24h", stress_no_charge_and_no_discharge_24h),
]

