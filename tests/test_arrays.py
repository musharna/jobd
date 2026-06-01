"""Unit tests for job-array template expansion (jobd.arrays)."""

from jobd.arrays import index_subs, render_cmd, render_env, render_template


def test_render_template_substitutes_index():
    assert render_template("--fold {i}", {"i": "3"}) == "--fold 3"


def test_render_template_leaves_unmatched_braces_untouched():
    # JSON literal in an arg must survive — this is why we use .replace, not
    # str.format (which would raise KeyError on {"a": 1} or mangle {} ).
    arg = '--config={"lr": 0.1, "n": {i}}'
    assert render_template(arg, {"i": "7"}) == '--config={"lr": 0.1, "n": 7}'


def test_render_template_no_key_present_is_noop():
    assert render_template("python train.py", {"i": "2"}) == "python train.py"


def test_render_template_multiple_keys_for_future_sweep():
    # The engine generalizes to named sweep axes with no code change.
    out = render_template("--lr {lr} --seed {seed}", {"lr": "0.01", "seed": "5"})
    assert out == "--lr 0.01 --seed 5"


def test_render_template_repeated_key_replaces_all_occurrences():
    assert render_template("{i}-{i}", {"i": "4"}) == "4-4"


def test_render_cmd_applies_per_arg():
    cmd = ["python", "train.py", "--fold", "{i}", "--out", "run-{i}/"]
    assert render_cmd(cmd, index_subs(2)) == [
        "python",
        "train.py",
        "--fold",
        "2",
        "--out",
        "run-2/",
    ]


def test_render_env_substitutes_values_not_keys():
    env = {"FOLD": "{i}", "STATIC": "x"}
    assert render_env(env, index_subs(6)) == {"FOLD": "6", "STATIC": "x"}


def test_index_subs_is_zero_based_string():
    assert index_subs(0) == {"i": "0"}
    assert index_subs(11) == {"i": "11"}
