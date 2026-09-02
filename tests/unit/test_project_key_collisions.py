"""audit 2026-09-02 T-3: the two branches that handle registered names which
fold onto the same key (`Foo` and `foo`) were executed by no test, so a
mutation that picked the first match -- routing one project's jobs at its
neighbour's priority, the failure this matching exists to end -- survived the
whole suite.
"""

import logging

from jobd.config import ProjectEntry, canonical_project_name, load_effective_projects


def test_an_ambiguous_fold_degrades_to_exact_matching_and_says_so(caplog):
    projects = {
        "_default": ProjectEntry(priority=40),
        "Foo": ProjectEntry(priority=90),
        "foo": ProjectEntry(priority=10),
    }
    with caplog.at_level(logging.WARNING, logger="jobd.config"):
        assert canonical_project_name(projects, "FOO") == "FOO"
    assert "folds onto more than one" in caplog.text
    # Positive control: an exact spelling is still found without a warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jobd.config"):
        assert canonical_project_name(projects, "Foo") == "Foo"
    assert caplog.text == ""


def test_loading_a_table_with_colliding_names_warns(tmp_path, caplog):
    projects = tmp_path / "projects.yaml"
    projects.write_text(
        "projects:\n  arf-promoter: { priority: 60 }\n  arf_promoter: { priority: 50 }\n"
    )
    with caplog.at_level(logging.WARNING, logger="jobd.config"):
        load_effective_projects(projects, tmp_path / "overrides.yaml")
    assert "differ only by case or -/_" in caplog.text
    assert "arf-promoter" in caplog.text and "arf_promoter" in caplog.text
