import importlib.metadata

from conftest import pin_drift


def test_pin_drift_names_only_unsatisfied_exact_pins(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# synthetic pins\nmatching==1.0\ndrifted==2.0  # note\nabsent==3.0\nranged>=1.0\n",
        encoding="utf-8",
    )
    installed = {"matching": "1.0", "drifted": "1.9", "ranged": "0.1"}

    def version(name):
        if name not in installed:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed[name]

    assert pin_drift(requirements, version) == [
        "drifted 1.9 (pinned 2.0)", "absent missing (pinned 3.0)",
    ]
