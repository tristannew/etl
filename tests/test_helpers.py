import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from owid import catalog

from etl import paths
from etl.helpers import PathFinder, create_dataset, get_comments_above_step_in_dag, isolated_env, write_to_dag_file


def test_PathFinder_paths():
    def _assert(pf):
        assert pf.channel == "meadow"
        assert pf.namespace == "papers"
        assert pf.version == "2022-11-03"
        assert pf.short_name == "zijdeman_et_al_2015"

    # saved as short_name/__init__.py
    pf = PathFinder(str(paths.STEP_DIR / "data/meadow/papers/2022-11-03/zijdeman_et_al_2015/__init__.py"))
    _assert(pf)
    assert pf.directory == paths.STEP_DIR / "data/meadow/papers/2022-11-03/zijdeman_et_al_2015"

    # saved as short_name/anymodule.py
    pf = PathFinder(str(paths.STEP_DIR / "data/meadow/papers/2022-11-03/zijdeman_et_al_2015/anymodule.py"))
    _assert(pf)
    assert pf.directory == paths.STEP_DIR / "data/meadow/papers/2022-11-03/zijdeman_et_al_2015"

    # saved as short_name.py
    pf = PathFinder(str(paths.STEP_DIR / "data/meadow/papers/2022-11-03/zijdeman_et_al_2015.py"))
    _assert(pf)
    assert pf.directory == paths.STEP_DIR / "data/meadow/papers/2022-11-03"


def test_create_dataset(tmp_path):
    meta = catalog.DatasetMeta(title="Test title")

    dest_dir = tmp_path / "data/garden/flowers/2020-01-01/rose"
    dest_dir.parent.mkdir(parents=True)

    # create metadata YAML file
    step_dir = tmp_path / "etl/steps"
    meta_yml = step_dir / "data/garden/flowers/2020-01-01/rose.meta.yml"
    meta_yml.parent.mkdir(parents=True)
    meta_yml.write_text(
        """
dataset:
    description: Test description
tables: {}""".strip()
    )

    # create dataset
    with patch("etl.paths.STEP_DIR", step_dir):
        ds = create_dataset(dest_dir, tables=[], default_metadata=meta)

    # check metadata
    assert ds.metadata.channel == "garden"
    assert ds.metadata.namespace == "flowers"
    assert ds.metadata.version == "2020-01-01"
    assert ds.metadata.short_name == "rose"
    assert ds.metadata.description == "Test description"
    assert ds.metadata.title == "Test title"


def test_PathFinder_with_private_steps():
    pf = PathFinder(str(paths.STEP_DIR / "data/garden/namespace/2023/name/__init__.py"))

    pf.dag = {
        "data://garden/namespace/2023/name": {
            "snapshot://namespace/2023/snapshot_a",
            "snapshot-private://namespace/2023/snapshot_b",
            # There could be two steps with the same name, one public and one private (odd case).
            "snapshot-private://namespace/2023/snapshot_a",
        }
    }
    assert pf.step == "data://garden/namespace/2023/name"
    assert pf.get_dependency_step_name("snapshot_a") == "snapshot://namespace/2023/snapshot_a"
    assert pf.get_dependency_step_name("snapshot_b") == "snapshot-private://namespace/2023/snapshot_b"
    # In the odd case that two dependencies have the same name, but one is public and the other is private,
    # assume it's public, unless explicitly stated otherwise.
    assert pf.get_dependency_step_name("snapshot_a", is_private=True) == "snapshot-private://namespace/2023/snapshot_a"

    pf.dag = {
        "data-private://garden/namespace/2023/name": {
            "snapshot-private://namespace/2023/name",
        }
    }
    assert pf.step == "data-private://garden/namespace/2023/name"
    assert pf.get_dependency_step_name("name") == "snapshot-private://namespace/2023/name"


def test_isolated_env(tmp_path):
    (tmp_path / "shared.py").write_text("A = 1; import test_abc")
    (tmp_path / "test_abc.py").write_text("B = 1")

    with isolated_env(tmp_path):
        import shared  # type: ignore

        assert shared.A == 1
        assert shared.test_abc.B == 1
        assert "test_abc" in sys.modules.keys()

    with pytest.raises(ModuleNotFoundError):
        import shared  # type: ignore

    assert "test_abc" not in sys.modules.keys()


def _assert_write_to_dag_file(
    old_content, expected_content, dag_part, comments=None, indent_step=2, indent_dependency=4
):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file = Path(temp_dir) / "temp.yml"
        # Create a dag file inside a temporary folder.
        temp_file.write_text(old_content)
        # Update dag file with the given dag part.
        write_to_dag_file(
            dag_file=temp_file,
            dag_part=dag_part,
            comments=comments,
            indent_step=indent_step,
            indent_dependency=indent_dependency,
        )
        # Assert that the file content is the same as before.
        with open(temp_file, "r") as updated_file:
            new_content = updated_file.read()
    assert new_content == expected_content


def test_write_to_dag_file_empty_dag_part():
    old_content = """\
steps:
    meadow_a:
    - snapshot_a
    meadow_b:
    - snapshot_b
"""
    expected_content = old_content
    _assert_write_to_dag_file(old_content, expected_content, dag_part={})


def test_write_to_dag_file_add_new_step():
    old_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
"""
    expected_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
  meadow_c:
    - snapshot_a
    - snapshot_b
"""
    _assert_write_to_dag_file(old_content, expected_content, dag_part={"meadow_c": ["snapshot_a", "snapshot_b"]})


def test_write_to_dag_file_update_existing_step():
    old_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
"""
    expected_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
    - snapshot_c
"""
    _assert_write_to_dag_file(old_content, expected_content, dag_part={"meadow_b": ["snapshot_b", "snapshot_c"]})


def test_write_to_dag_file_change_indent():
    old_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
  meadow_c:
    - snapshot_a
    - snapshot_b
"""
    expected_content = """\
steps:
  meadow_a:
    - snapshot_a
  meadow_b:
    - snapshot_b
  meadow_c:
    - snapshot_a
    - snapshot_b
   meadow_d:
     - snapshot_d
"""
    _assert_write_to_dag_file(
        old_content, expected_content, dag_part={"meadow_d": ["snapshot_d"]}, indent_step=3, indent_dependency=5
    )


def test_write_to_dag_file_respect_comments():
    old_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    - snapshot_b
"""
    expected_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    - snapshot_b
  meadow_c:
    - snapshot_a
    - snapshot_b
"""
    _assert_write_to_dag_file(old_content, expected_content, dag_part={"meadow_c": ["snapshot_a", "snapshot_b"]})


def test_write_to_dag_file_respect_line_breaks():
    old_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a

  # Comment for meadow_b.

  # And another comment.
  meadow_b:

    - snapshot_b
"""
    expected_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a

  # Comment for meadow_b.

  # And another comment.
  meadow_b:

    - snapshot_b
  meadow_c:
    - snapshot_a
    - snapshot_b
"""
    _assert_write_to_dag_file(old_content, expected_content, dag_part={"meadow_c": ["snapshot_a", "snapshot_b"]})


def test_write_to_dag_file_remove_comments_within_updated_dependencies():
    old_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b
"""
    # NOTE: This is not necessarily desired behavior, but it is the one to be expected.
    # Keeping track of comments among dependencies may be a bit trickier.
    expected_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    - snapshot_b
"""
    _assert_write_to_dag_file(old_content, expected_content, dag_part={"meadow_b": ["snapshot_b"]})


def test_write_to_dag_file_add_comments():
    old_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b
"""
    expected_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b
  # Comment for meadow_c.
  meadow_c:
    - snapshot_a
    - snapshot_b
"""
    _assert_write_to_dag_file(
        old_content,
        expected_content,
        dag_part={"meadow_c": ["snapshot_a", "snapshot_b"]},
        comments={"meadow_c": "# Comment for meadow_c."},
    )


def test_write_to_dag_file_with_include_section():
    old_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b
include:
  - path/to/another/dag.yml
"""
    # NOTE: By construction, we impose that there must be an empty space between steps and include sections.
    expected_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b
  # Comment for meadow_c.
  meadow_c:
    - snapshot_a
    - snapshot_b

include:
  - path/to/another/dag.yml
"""
    _assert_write_to_dag_file(
        old_content,
        expected_content,
        dag_part={"meadow_c": ["snapshot_a", "snapshot_b"]},
        comments={"meadow_c": "# Comment for meadow_c."},
    )


def test_get_comments_above_step_in_dag():
    yaml_content = """\
steps:
  # Comment for meadow_a.
  meadow_a:
    # Comment for snapshot_a.
    - snapshot_a
  # Comment for meadow_b.
  # And another comment.
  meadow_b:
    # Comment for snapshot_b.
    - snapshot_b

  meadow_c:
    - snapshot_a
    - snapshot_b
  #
  meadow_d:

  # Comment for meadow_e.

  meadow_e:
    - snapshot_e

include:
  - path/to/another/dag.yml
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_file = Path(temp_dir) / "temp.yml"
        # Create a dag file inside a temporary folder.
        temp_file.write_text(yaml_content)
        assert get_comments_above_step_in_dag(step="meadow_a", dag_file=temp_file) == "# Comment for meadow_a.\n"
        assert (
            get_comments_above_step_in_dag(step="meadow_b", dag_file=temp_file)
            == "# Comment for meadow_b.\n# And another comment.\n"
        )
        assert get_comments_above_step_in_dag(step="meadow_c", dag_file=temp_file) == ""
        assert get_comments_above_step_in_dag(step="meadow_d", dag_file=temp_file) == "#\n"
        assert get_comments_above_step_in_dag(step="meadow_e", dag_file=temp_file) == "# Comment for meadow_e.\n"
        assert get_comments_above_step_in_dag(step="non_existing_step", dag_file=temp_file) == ""
