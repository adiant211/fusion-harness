import pytest

from fusion_harness import tree


def good_tree():
    t = tree.base_tree()
    assert t, "the seed harness ships with the package"
    return t


def test_seed_harness_is_valid_and_lean():
    census = tree.validate(good_tree())
    assert census["skills"] == 2 and census["tools"] == 1
    assert census["always_on_chars"] < 4000


def test_untriggered_skill_is_rejected_then_autotriggered():
    t = good_tree()
    t["skills/lint/SKILL.md"] = "---\nname: lint\ndescription: Code needs linting\n---\nRun ruff."
    with pytest.raises(ValueError, match="never named in always-on"):
        tree.validate(t)
    assert tree.autotrigger(t) == ["lint"]
    assert "load the `lint` skill" in t["guardrails.md"]
    tree.validate(t)


def test_skill_needs_description_and_matching_name():
    t = good_tree()
    t["skills/x/SKILL.md"] = "---\nname: y\n---\nbody"
    t["guardrails.md"] += "\nWhen needed, load the `x` skill."
    msg = str(pytest.raises(ValueError, tree.validate, t).value)
    assert "description" in msg and "name must be 'x'" in msg


def test_tool_header_is_parsed_without_executing():
    src = "TOOL = {'name': 'count', 'description': 'd', 'input_schema': {'type': 'object'}}\n" \
          "raise SystemExit('must not run at validation')\n"
    assert tree.tool_spec(src)["name"] == "count"
    t = good_tree()
    t["tools/count.py"] = src
    tree.validate(t)


@pytest.mark.parametrize("src,needle", [
    ("x = 1\n", "no `TOOL"),
    ("TOOL = {'name': 'other', 'description': 'd', 'input_schema': {'type': 'object'}}\n",
     "must be 'count'"),
    ("TOOL = {'name': 'count', 'description': '', 'input_schema': {'type': 'object'}}\n",
     "non-empty 'description'"),
    ("def broken(:\n", "does not compile"),
])
def test_bad_tools_are_rejected(src, needle):
    t = good_tree()
    t["tools/count.py"] = src
    with pytest.raises(ValueError, match=needle):
        tree.validate(t)


def test_builtin_tool_names_are_reserved():
    t = good_tree()
    t["tools/bash.py"] = "TOOL = {'name': 'bash', 'description': 'd', 'input_schema': {'type': 'object'}}\n"
    with pytest.raises(ValueError, match="built-in"):
        tree.validate(t)


def test_missing_script_reference_and_always_on_cap():
    t = good_tree()
    t["tools.md"] += "\nRun python $FH_HARNESS/scripts/check_totals.py first."
    with pytest.raises(ValueError, match="scripts/check_totals.py"):
        tree.validate(t)
    t["scripts/check_totals.py"] = "print('ok')\n"
    tree.validate(t)
    t["memory.md"] = "x" * 20_000
    with pytest.raises(ValueError, match="over the 12000 cap"):
        tree.validate(t)


def test_read_write_round_trip_and_off_surface_files(tmp_path):
    t = good_tree()
    tree.write_tree(t, tmp_path / "h")
    (tmp_path / "h" / "notes.txt").write_text("stray")
    (tmp_path / "h" / "skills" / "Bad Name").mkdir(parents=True)
    rejected: list[str] = []
    back = tree.read_tree(tmp_path / "h", rejected=rejected)
    assert back == t
    assert rejected == ["notes.txt"]


def test_path_escape_is_refused():
    assert not tree.is_allowed("../system.md")
    with pytest.raises(ValueError):
        tree.normalize_rel("/etc/passwd")


def test_fingerprint_depends_on_domain_and_content():
    t = good_tree()
    assert tree.fingerprint("a", t) != tree.fingerprint("b", t)
    t2 = dict(t, **{"memory.md": "changed"})
    assert tree.fingerprint("a", t) != tree.fingerprint("a", t2)
    assert tree.unchanged(t, dict(t))
