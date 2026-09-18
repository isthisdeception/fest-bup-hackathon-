"""
Tests for app.llm.interpreter: JSON extraction and fallback modes.
"""

from app.llm.interpreter import parse_model_json, parse_model_json_with_mode


def test_parse_model_json_direct():
    t = '{"interpretations": []}'
    obj, mode = parse_model_json_with_mode(t)
    assert obj == {"interpretations": []}
    assert mode == "direct"
    assert parse_model_json(t) == {"interpretations": []}


def test_parse_model_json_fenced():
    t = '```json\n{"interpretations": []}\n```'
    obj, mode = parse_model_json_with_mode(t)
    assert obj == {"interpretations": []}
    assert mode == "fenced"


def test_parse_model_json_prose_wrapped():
    t = 'Here is the result: {"a": 1} and done.'
    obj, mode = parse_model_json_with_mode(t)
    assert obj == {"a": 1}
    assert mode == "outer_object"


def test_parse_model_json_bare_array():
    t = 'The array is: [{"note_index": 0}] end'
    obj, mode = parse_model_json_with_mode(t)
    assert obj == [{"note_index": 0}]
    assert mode == "outer_array"


def test_parse_model_json_invalid_string():
    t = "sorry, I cannot help with that"
    obj, mode = parse_model_json_with_mode(t)
    assert obj is None
    assert mode == "failed"
    assert parse_model_json(t) is None


def test_interpret_validated_repair_retry(monkeypatch):
    import app.llm.interpreter as interp
    from tests.fixtures import flat_tariff_no_solar
    sc, _ = flat_tariff_no_solar()

    call_count = 0

    def mock_interpret(notes, scenario, deadline=None, feedback=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Omit note 1 to trigger retry
            return {
                "interpretations": [
                    {
                        "note_index": 0,
                        "directive_type": "solar_reduction",
                        "hours": [10, 11],
                        "factor": 0.5,
                        "minimum_energy_kwh": None,
                        "max_grid_kwh": None,
                        "explanation": "half solar",
                    }
                ]
            }, {"parse_mode": "direct"}
        else:
            return {
                "interpretations": [
                    {
                        "note_index": 0,
                        "directive_type": "solar_reduction",
                        "hours": [10, 11],
                        "factor": 0.5,
                        "minimum_energy_kwh": None,
                        "max_grid_kwh": None,
                        "explanation": "half solar",
                    },
                    {
                        "note_index": 1,
                        "directive_type": "no_op",
                        "hours": [],
                        "factor": None,
                        "minimum_energy_kwh": None,
                        "max_grid_kwh": None,
                        "explanation": "no op",
                    },
                ]
            }, {"parse_mode": "direct"}

    monkeypatch.setattr(interp, "interpret", mock_interpret)
    directives, diag = interp.interpret_validated(["solar cut", "announcement"], sc)
    assert call_count == 2
    assert diag["attempts"] == 2
    assert diag["repaired"] is True
    assert len(directives) == 2
    assert directives[0].directive_type == "solar_reduction"
    assert directives[1].directive_type == "no_op"


def test_interpret_validated_outage_degradation(monkeypatch):
    import app.llm.interpreter as interp
    from tests.fixtures import flat_tariff_no_solar
    sc, _ = flat_tariff_no_solar()

    def mock_interpret(*args, **kwargs):
        raise interp.LLMConfigError("HTTP 400: API_KEY_INVALID")

    monkeypatch.setattr(interp, "interpret", mock_interpret)
    directives, diag = interp.interpret_validated(["note 0", "note 1"], sc)
    assert diag["degraded"] is True
    assert len(directives) == 2
    assert all(d.directive_type == "no_op" for d in directives)


def test_interpret_validated_backup_provider(monkeypatch):
    import app.llm.interpreter as interp
    from app.config import settings
    from tests.fixtures import flat_tariff_no_solar
    sc, _ = flat_tariff_no_solar()

    monkeypatch.setattr(settings, "backup_llm_provider", "backup_mock")
    monkeypatch.setattr(settings, "backup_llm_api_key", "backup_key")
    monkeypatch.setattr(settings, "backup_llm_model", "backup_model")

    called_providers = []

    def mock_interpret(notes, scenario, deadline=None, feedback=None, provider=None, **kwargs):
        called_providers.append(provider or "primary")
        if provider is None:
            raise interp.LLMProviderError("Primary 503 error")
        return {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "no_op",
                    "hours": [],
                    "factor": None,
                    "minimum_energy_kwh": None,
                    "max_grid_kwh": None,
                    "explanation": "backup response",
                }
            ]
        }, {"parse_mode": "direct"}

    monkeypatch.setattr(interp, "interpret", mock_interpret)
    directives, diag = interp.interpret_validated(["note 0"], sc)
    assert called_providers == ["primary", "backup_mock"]
    assert len(directives) == 1
    assert directives[0].directive_type == "no_op"


if __name__ == "__main__":
    test_parse_model_json_direct()
    test_parse_model_json_fenced()
    test_parse_model_json_prose_wrapped()
    test_parse_model_json_bare_array()
    test_parse_model_json_invalid_string()
    print("All parse_model_json tests PASSED!")

