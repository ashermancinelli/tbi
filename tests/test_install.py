from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tbi.cli import (
    ARCHIVE_FILE_RE,
    SKIP_RE,
    TbiError,
    clean_yaml_scalar,
    find_install_sources,
    has_glob_chars,
    install_candidates,
    install_mapped_paths,
    safe_relative_path,
)


# ---------------------------------------------------------------------------
# has_glob_chars
# ---------------------------------------------------------------------------

class TestHasGlobChars:
    def test_star(self):
        assert has_glob_chars("cli*")

    def test_question(self):
        assert has_glob_chars("cli?")

    def test_bracket(self):
        assert has_glob_chars("cli[0-9]")

    def test_plain_string(self):
        assert not has_glob_chars("cli")

    def test_empty(self):
        assert not has_glob_chars("")

    def test_path_with_glob(self):
        assert has_glob_chars("bin/cli*")

    def test_path_no_glob(self):
        assert not has_glob_chars("bin/cli")


# ---------------------------------------------------------------------------
# safe_relative_path
# ---------------------------------------------------------------------------

class TestSafeRelativePath:
    def test_simple(self):
        assert safe_relative_path("bin/render", "install destination") == Path("bin/render")

    def test_single_component(self):
        assert safe_relative_path("cli", "install source") == Path("cli")

    def test_nested(self):
        assert safe_relative_path("a/b/c", "whatever") == Path("a/b/c")

    def test_absolute_rejected(self):
        with pytest.raises(TbiError, match="unsafe"):
            safe_relative_path("/usr/local/bin", "install destination")

    def test_dotdot_rejected(self):
        with pytest.raises(TbiError, match="unsafe"):
            safe_relative_path("../etc/passwd", "install source")


# ---------------------------------------------------------------------------
# clean_yaml_scalar
# ---------------------------------------------------------------------------

class TestCleanYamlScalar:
    def test_unquoted(self):
        assert clean_yaml_scalar("  cli*  ") == "cli*"

    def test_single_quoted(self):
        assert clean_yaml_scalar("'cli*'") == "cli*"

    def test_double_quoted(self):
        assert clean_yaml_scalar('"cli*"') == "cli*"

    def test_inline_comment(self):
        assert clean_yaml_scalar("bin/render  # some comment") == "bin/render"

    def test_quoted_with_comment(self):
        assert clean_yaml_scalar("'bin/render'  # comment") == "bin/render"

    def test_empty(self):
        assert clean_yaml_scalar("") == ""


# ---------------------------------------------------------------------------
# ARCHIVE_FILE_RE
# ---------------------------------------------------------------------------

class TestArchiveFileRe:
    @pytest.mark.parametrize(
        "name",
        [
            "foo.zip",
            "foo.tar.gz",
            "foo.tgz",
            "foo.tar.xz",
            "foo.txz",
            "foo.tar.bz2",
            "foo.tbz",
            "cli_v2.20.0_darwin_amd64.zip",
        ],
    )
    def test_matches(self, name):
        assert ARCHIVE_FILE_RE.search(name), f"{name!r} should be recognised as archive"

    @pytest.mark.parametrize(
        "name",
        [
            "cli_v2.20.0",
            "render",
            "bin",
            "lib",
            "foo.tar",
            "foo.gz",
            "README.md",
            "LICENSE",
            "foo.txt",
        ],
    )
    def test_non_matches(self, name):
        assert not ARCHIVE_FILE_RE.search(name), f"{name!r} should not be recognised as archive"


# ---------------------------------------------------------------------------
# SKIP_RE
# ---------------------------------------------------------------------------

class TestSkipRe:
    @pytest.mark.parametrize(
        "name",
        ["LICENSE", "license", "README.md", "readme.txt", "CHANGELOG", "notes.txt", "docs.md"],
    )
    def test_matches(self, name):
        assert SKIP_RE.match(name), f"{name!r} should be skipped"

    @pytest.mark.parametrize("name", ["cli_v2.20.0", "render", "bin", "lib", "nvim", "nvim.appimage"])
    def test_non_matches(self, name):
        assert not SKIP_RE.match(name), f"{name!r} should not be skipped"


# ---------------------------------------------------------------------------
# find_install_sources  – glob matching
# ---------------------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "workdir"
    d.mkdir()

    # actual binary
    (d / "cli_v2.20.0").write_text("binary content")
    (d / "cli_v2.20.0").chmod(0o755)

    # archive file (should be filtered)
    (d / "cli_v2.20.0_darwin_amd64.zip").write_text("zip content")

    # skip file (should be filtered)
    (d / "README.md").write_text("readme")
    (d / "LICENSE").write_text("license")

    # unrelated file (should not match cli*)
    (d / "other_tool").write_text("other")
    (d / "other_tool").chmod(0o755)

    # subdirectory with matching content (first-level only)
    sub = d / "extracted_dir"
    sub.mkdir()
    (sub / "nested_binary").write_text("nested")
    (sub / "nested_binary").chmod(0o755)
    (sub / "cli_v1.0.0").write_text("versioned")
    (sub / "cli_v1.0.0").chmod(0o755)

    # subdirectory that also matches glob pattern (should be skipped)
    sub2 = d / "cli_somedir"
    sub2.mkdir()
    (sub2 / "dummy").write_text("dummy")
    (sub2 / "dummy").chmod(0o755)

    # deeply nested (second level – should NOT be checked)
    deep = sub / "nested"
    deep.mkdir()
    (deep / "cli_deep").write_text("deep")
    (deep / "cli_deep").chmod(0o755)

    return d


class TestFindInstallSourcesGlob:
    def test_glob_matches_the_binary(self, workdir: Path):
        result = find_install_sources(workdir, "cli*")
        names = {p.name for p in result}
        assert "cli_v2.20.0" in names, "the real binary should be matched"

    def test_glob_skips_archives(self, workdir: Path):
        result = find_install_sources(workdir, "cli*")
        names = {p.name for p in result}
        assert "cli_v2.20.0_darwin_amd64.zip" not in names, "zip should be filtered out"

    def test_glob_skips_directories(self, workdir: Path):
        result = find_install_sources(workdir, "cli*")
        assert all(p.is_file() for p in result), "directories should not be included in glob results"
        names = {p.name for p in result}
        assert "cli_somedir" not in names, "directories matching the pattern should be filtered"

    def test_glob_skips_skip_re_files(self, workdir: Path):
        result = find_install_sources(workdir, "*")
        names = {p.name for p in result}
        assert "README.md" not in names
        assert "LICENSE" not in names

    def test_glob_matches_in_first_level_subdirs(self, workdir: Path):
        result = find_install_sources(workdir, "cli*")
        names = {p.name for p in result}
        assert "cli_v1.0.0" in names, "files in first-level subdirs should be matched"

    def test_glob_does_not_search_deeply_nested(self, workdir: Path):
        result = find_install_sources(workdir, "cli*")
        names = {p.name for p in result}
        assert "cli_deep" not in names, "second-level subdirs should not be checked"

    def test_no_match_dies(self, workdir: Path):
        with pytest.raises(TbiError, match="cli_v999"):
            find_install_sources(workdir, "cli_v999")

    def test_pattern_question_mark(self, workdir: Path):
        (workdir / "abcd").write_text("data")
        (workdir / "abcd").chmod(0o755)
        (workdir / "abxd").write_text("data")
        (workdir / "abxd").chmod(0o755)
        (workdir / "ab").write_text("data")
        (workdir / "ab").chmod(0o755)
        result = find_install_sources(workdir, "abcd")
        assert len(result) == 1
        result = find_install_sources(workdir, "ab?d")
        names = {p.name for p in result}
        assert "abcd" in names
        assert "abxd" in names
        assert "ab" not in names

    def test_pattern_bracket(self, workdir: Path):
        (workdir / "foo1").write_text("data")
        (workdir / "foo1").chmod(0o755)
        (workdir / "foo2").write_text("data")
        (workdir / "foo2").chmod(0o755)
        (workdir / "foo9").write_text("data")
        (workdir / "foo9").chmod(0o755)
        result = find_install_sources(workdir, "foo[0-9]")
        names = {p.name for p in result}
        assert "foo1" in names
        assert "foo2" in names
        assert "foo9" in names


# ---------------------------------------------------------------------------
# find_install_sources – exact matching (no glob chars)
# ---------------------------------------------------------------------------

class TestFindInstallSourcesExact:
    def test_file_in_root(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        (d / "cli").write_text("binary")
        (d / "cli").chmod(0o755)
        (d / "cli_darwin_amd64.zip").write_text("zip")

        result = find_install_sources(d, "cli")
        assert len(result) == 1
        assert result[0].name == "cli"

    def test_file_in_subdir(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        sub = d / "some_dir"
        sub.mkdir()
        (sub / "target_file").write_text("found")
        (sub / "target_file").chmod(0o755)

        result = find_install_sources(d, "target_file")
        assert len(result) == 1

    def test_directory(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        (d / "bin").mkdir()
        result = find_install_sources(d, "bin")
        assert len(result) == 1
        assert result[0].is_dir()

    def test_no_match_exact_dies(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        with pytest.raises(TbiError, match="nonexistent"):
            find_install_sources(d, "nonexistent")


# ---------------------------------------------------------------------------
# install_candidates
# ---------------------------------------------------------------------------

class TestInstallCandidates:
    def test_returns_executables(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        (d / "exec1").write_text("bin")
        (d / "exec1").chmod(0o755)
        (d / "exec2").write_text("bin")
        (d / "exec2").chmod(0o755)
        (d / "not_exec").write_text("bin")

        result = install_candidates(d)
        names = {p.name for p in result}
        assert "exec1" in names
        assert "exec2" in names
        assert "not_exec" not in names

    def test_prefers_bin_subdir(self, tmp_path: Path):
        d = tmp_path / "workdir"
        d.mkdir()
        (d / "exec_root").write_text("bin")
        (d / "exec_root").chmod(0o755)
        (d / "bin").mkdir()
        (d / "bin" / "exec_sub").write_text("bin")
        (d / "bin" / "exec_sub").chmod(0o755)

        result = install_candidates(d)
        # should prefer bin/ entries
        names = {p.name for p in result}
        assert "exec_sub" in names
        assert "exec_root" not in names


# ---------------------------------------------------------------------------
# install_mapped_paths
# ---------------------------------------------------------------------------

@pytest.fixture
def install_prefix(tmp_path: Path) -> Path:
    return tmp_path / "prefix"


def noop_say(_msg: str) -> None:
    pass


class TestInstallMappedPaths:
    def test_file_rename_with_glob(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "cli_v2.20.0").write_text("binary")
        (workdir / "cli_v2.20.0").chmod(0o755)

        rules = {"cli*": "bin/render"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        assert names == ["render"]

        installed = install_prefix / "bin" / "render"
        assert installed.is_file(), f"expected {installed} to exist"
        assert installed.read_text() == "binary"

    def test_directory_mapping(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "bin").mkdir()
        (workdir / "bin" / "tool").write_text("tool binary")
        (workdir / "bin" / "tool").chmod(0o755)
        (workdir / "lib").mkdir()
        (workdir / "lib" / "libfoo.so").write_text("lib content")
        (workdir / "lib" / "libfoo.so").chmod(0o755)

        rules = {"bin": "bin", "lib": "lib"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        # only bin/ destinations are tracked in names
        assert names == ["tool"]

        assert (install_prefix / "bin" / "tool").is_file()

    def test_exact_file_mapping(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "foobar").write_text("exact binary")
        (workdir / "foobar").chmod(0o755)

        rules = {"foobar": "bin/my_tool"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        assert names == ["my_tool"]
        assert (install_prefix / "bin" / "my_tool").is_file()

    def test_multiple_matches_glob(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "tool-v1").write_text("v1")
        (workdir / "tool-v1").chmod(0o755)
        (workdir / "tool-v2").write_text("v2")
        (workdir / "tool-v2").chmod(0o755)

        rules = {"tool-v*": "bin/deploy"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        # sorted: tool-v1 then tool-v2 -> v2 wins
        installed = install_prefix / "bin" / "deploy"
        assert installed.is_file()
        # both copied in sorted order; last write wins
        assert installed.read_text() == "v2"

    def test_unsafe_destination_rejected(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "foo").write_text("data")
        (workdir / "foo").chmod(0o755)

        with pytest.raises(TbiError, match="unsafe"):
            install_mapped_paths("test", workdir, install_prefix, {"foo": "../evil"}, noop_say)

    def test_multiple_rules(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "cli_v1").write_text("cli data")
        (workdir / "cli_v1").chmod(0o755)
        (workdir / "helper").write_text("helper data")
        (workdir / "helper").chmod(0o755)

        rules = {"cli*": "bin/my_cli", "helper": "bin/helper_tool"}
        names = sorted(install_mapped_paths("test", workdir, install_prefix, rules, noop_say))
        assert names == sorted(["my_cli", "helper_tool"])

    def test_archive_file_filtered_by_install_mapped_paths(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "cli_v2.20.0").write_text("binary")
        (workdir / "cli_v2.20.0").chmod(0o755)
        (workdir / "cli_v2.20.0_darwin_amd64.zip").write_text("zip content")

        rules = {"cli*": "bin/render"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        assert names == ["render"]
        installed = install_prefix / "bin" / "render"
        assert installed.is_file()
        assert installed.read_text() == "binary", "should install the binary, not the zip"

    def test_skip_re_files_not_installed(self, tmp_path: Path, install_prefix: Path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "cli_bin").write_text("binary")
        (workdir / "cli_bin").chmod(0o755)
        (workdir / "README.md").write_text("readme")
        (workdir / "LICENSE").write_text("license")

        rules = {"*": "bin/dest"}
        names = install_mapped_paths("test", workdir, install_prefix, rules, noop_say)
        assert names == ["dest"]
        assert not (install_prefix / "bin" / "README.md").exists()
        assert not (install_prefix / "bin" / "LICENSE").exists()


# ---------------------------------------------------------------------------
# Integration test – requires GITHUB_PAT
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestInstallIntegration:
    def test_install_render_as_renamed(self, monkeypatch):
        pat = os.environ.get("GITHUB_PAT")
        if not pat:
            pytest.skip("GITHUB_PAT not set")

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cache_dir = tmpdir / "cache"
            install_dir = tmpdir / "local" / "bin"
            install_dir.mkdir(parents=True)

            monkeypatch.setenv("TBI_CACHE_DIR", str(cache_dir))
            monkeypatch.setenv("TBI_INSTALL_DIR", str(install_dir))

            from tbi.cli import install

            ns = type("Args", (), {
                "targets": ["render"],
                "prefix": str(tmpdir / "local"),
                "tag": "latest",
                "keep_temp": False,
                "unattended": True,
                "unattended_select_index": 1,
            })()

            ret = install(ns)
            assert ret == 0, f"install returned non-zero: {ret}"

            installed = install_dir / "render"
            assert installed.is_file(), f"expected render binary at {installed}"
            assert os.access(installed, os.X_OK), f"{installed} should be executable"
            assert installed.name == "render", f"expected 'render', got {installed.name!r}"

    def test_install_render_without_rules_falls_back(self, monkeypatch):
        """Simulate what happens when no install rules exist."""
        pat = os.environ.get("GITHUB_PAT")
        if not pat:
            pytest.skip("GITHUB_PAT not set")

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cache_dir = tmpdir / "cache"
            install_dir = tmpdir / "local" / "bin"
            install_dir.mkdir(parents=True)

            monkeypatch.setenv("TBI_CACHE_DIR", str(cache_dir))
            monkeypatch.setenv("TBI_INSTALL_DIR", str(install_dir))
            monkeypatch.setenv("TBI_UNATTENDED", "true")

            from tbi.cli import install

            ns = type("Args", (), {
                "targets": ["render-oss/cli"],
                "prefix": str(tmpdir / "local"),
                "tag": "latest",
                "keep_temp": False,
                "unattended": True,
                "unattended_select_index": 1,
            })()

            ret = install(ns)
            assert ret == 0
            bins = list(install_dir.iterdir())
            assert len(bins) >= 1, "expected at least one installed binary"
            for b in bins:
                assert os.access(b, os.X_OK), f"{b} should be executable"
