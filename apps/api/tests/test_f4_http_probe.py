import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.f4_engineering.http_probe import probe


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Client:
    def __init__(self):
        self.calls = []

    def post(self, path, *, json):
        self.calls.append((path, json))
        if path.endswith("/login"):
            return Response({"contexts": [{"ref": "secret-context", "label": "Selected child"}]})
        if path.endswith("/context"):
            return Response({"selected_context_kind": "guardian_relationship"})
        return Response(
            {
                "status": "available",
                "profile": {"teachers": [{"display_name": "Teacher", "teacher_role": "main"}]},
                "reference": "internal",
                "sources": [
                    {
                        "fact": "class_teacher_assignment",
                        "source_kind": "school_publication",
                        "source_ref": "private",
                    }
                ],
            }
        )


def test_probe_uses_selected_context_and_only_deterministic_read_routes():
    client = Client()
    result = probe(
        client,
        account_ref="guardian",
        context_label="Selected child",
        kind="guardian_relationship",
        payload={"fields": ["teachers"]},
    )
    assert result["model_calls"] == 0 and result["status"] == "available"
    assert result["profile"]["teachers"][0]["display_name"] == "Teacher"
    assert (
        "secret-context" not in str(result)
        and "private" not in str(result)
        and "internal" not in str(result)
    )
    assert [p.rsplit("/", 1)[-1] for p, _ in client.calls] == [
        "login",
        "context",
        "education",
        "logout",
    ]


def test_probe_fails_closed_for_wrong_selected_identity_and_logs_out():
    import pytest

    client = Client()
    with pytest.raises(ValueError, match="kind mismatch"):
        probe(
            client,
            account_ref="guardian",
            context_label="Selected child",
            kind="staff_assignment",
            payload={"fields": ["teachers"]},
        )
    assert not any(p.endswith("/lookup") or p.endswith("/education") for p, _ in client.calls)
    assert client.calls[-1][0].endswith("/logout")


def test_probe_never_selects_first_context_when_label_does_not_match():
    import pytest

    client = Client()
    with pytest.raises(ValueError, match="absent or ambiguous"):
        probe(
            client,
            account_ref="guardian",
            context_label="Other child",
            kind="guardian_relationship",
            payload={"fields": ["teachers"]},
        )
    assert not any(p.endswith("/context") for p, _ in client.calls)
