"""Client worktrees against real git repositories in a temporary directory.

No network and no `make install`: the install step and the delivery checks are
injected, everything else — worktrees, branches, rebases, tags, merges — is
real git, because that is where the failure modes are.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest

from personabot.application import clients
from personabot.application.clients import (
    ClientError,
    Git,
    base_keys,
    create_client,
    deliver_client,
    list_clients,
    locate,
    parse_key_selection,
    render_client_env,
    sync_client,
)
from personabot.settings import Settings

from conftest import REPO_ROOT

BASE_ENV = """# base
SURVEY="old_one"
AI_PROVIDER="gemini"
GEMINI_API_KEY_1="key-one"
GEMINI_API_KEY_2="key-two"
GEMINI_API_KEY_3="key-three"
BASE_FORM_URL="https://example.com/old"
USE_TOR="True"
TOR_SOCKS_EXTRA_PORTS="9052,9053"
"""


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for name, value in {
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def base(tmp_path) -> Path:
    """A minimal PersonaBot checkout: template, .gitignore, .env.example, .env."""
    root = tmp_path / "PersonaBot"
    (root / "surveys").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "surveys" / "_template", root / "surveys" / "_template")
    shutil.copy(REPO_ROOT / ".gitignore", root / ".gitignore")
    shutil.copy(REPO_ROOT / ".env.example", root / ".env.example")
    (root / "README.md").write_text("base\n")
    git = Git(root)
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    (root / ".env").write_text(BASE_ENV)        # ignored, like the real one
    return root


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, clients_dir=tmp_path / "clients")


def _new(settings, base, client="acme", survey="job_stress_fa", **kwargs):
    return create_client(settings, base, client, survey, install=False, **kwargs)


# --------------------------------------------------------------------- .env

class TestEnv:
    def test_key_selection(self):
        assert parse_key_selection(None, 4) == [1, 2, 3, 4]
        assert parse_key_selection("1-2,4", 4) == [1, 2, 4]
        with pytest.raises(ClientError, match="outside"):
            parse_key_selection("3-9", 4)
        with pytest.raises(ClientError, match="Bad key"):
            parse_key_selection("one", 4)

    def test_client_env_selects_and_renumbers_keys(self):
        worktree = clients.ClientWorktree("acme", "s1", Path("/x"))
        text = render_client_env(BASE_ENV, worktree=worktree, keys=["key-two", "key-three"])
        assert base_keys(text) == ["key-two", "key-three"]
        assert 'GEMINI_API_KEY_1="key-two"' in text

    def test_client_env_selects_its_own_survey_and_drops_urls(self):
        worktree = clients.ClientWorktree("acme", "s1", Path("/x"))
        text = render_client_env(BASE_ENV, worktree=worktree, keys=[])
        assert 'SURVEY="s1"' in text and "old_one" not in text
        assert "BASE_FORM_URL" not in text

    def test_tor_settings_are_inherited_unchanged(self):
        """Two clients may share exit IPs; only one client's own submissions
        must differ, which the SocksPort rotation already guarantees."""
        worktree = clients.ClientWorktree("acme", "s1", Path("/x"))
        text = render_client_env(BASE_ENV, worktree=worktree, keys=[])
        assert 'TOR_SOCKS_EXTRA_PORTS="9052,9053"' in text


# ---------------------------------------------------------------------- new

class TestNew:
    def test_creates_an_isolated_worktree_on_its_own_branch(self, settings, base, tmp_path):
        worktree = _new(settings, base, keys="2-3")

        assert worktree.path == tmp_path / "clients" / "acme" / "job_stress_fa"
        assert Git(worktree.path)("rev-parse", "--abbrev-ref", "HEAD") == "survey/job_stress_fa"
        env = (worktree.path / ".env").read_text()
        assert base_keys(env) == ["key-two", "key-three"]
        assert 'SURVEY="job_stress_fa"' in env
        assert (worktree.path / ".env").stat().st_mode & 0o777 == 0o600

    def test_the_survey_is_scaffolded_and_committed(self, settings, base):
        worktree = _new(settings, base)
        spec = worktree.path / "surveys" / "job_stress_fa" / "survey.yaml"
        assert "id: job_stress_fa" in spec.read_text()
        git = Git(worktree.path)
        assert git.dirty() == []
        assert "survey(job_stress_fa): start for client acme" in git("log", "-1", "--format=%s")

    def test_main_and_other_clients_do_not_see_the_survey(self, settings, base):
        _new(settings, base)
        other = _new(settings, base, client="beta", survey="burnout_fa")
        assert not (base / "surveys" / "job_stress_fa").exists()
        assert not (other.path / "surveys" / "job_stress_fa").exists()

    def test_a_survey_already_on_main_is_continued_not_recreated(self, settings, base):
        first = _new(settings, base)
        deliver_client(settings, base, first, checks=False, push=False)
        again = _new(settings, base)          # the client is back for another round
        assert (again.path / "surveys" / "job_stress_fa" / "survey.yaml").is_file()
        assert Git(again.path)("rev-list", "--count", "main..HEAD") == "0"

    def test_the_same_survey_twice_at_once_is_refused(self, settings, base):
        _new(settings, base)
        with pytest.raises(ClientError, match="already exists"):
            _new(settings, base, client="beta")

    def test_bad_names_are_refused(self, settings, base):
        with pytest.raises(ClientError, match="not usable"):
            _new(settings, base, client="../escape")

    def test_install_failure_is_reported_with_the_fix(self, settings, base):
        with pytest.raises(ClientError, match="make install"):
            create_client(settings, base, "acme", "s1", runner=lambda cmd, cwd: 1)


# --------------------------------------------------------------------- list

class TestList:
    def test_lists_clients_and_flags_shared_keys(self, settings, base):
        _new(settings, base, client="acme", survey="s1", keys="1-2")
        _new(settings, base, client="beta", survey="s2", keys="2-3")
        statuses = list_clients(settings, base)

        assert [s.worktree.label for s in statuses] == ["acme/s1", "beta/s2"]
        assert [s.ahead for s in statuses] == [1, 1]
        text = clients.render_clients(statuses)
        assert "share 1 key(s)" in text

    def test_locate_reads_client_and_survey_off_the_path(self, settings, base):
        worktree = _new(settings, base)
        found = locate(settings, base, None, None, cwd=worktree.path / "surveys")
        assert (found.client, found.survey) == ("acme", "job_stress_fa")

    def test_locate_outside_a_worktree_explains_itself(self, settings, base):
        with pytest.raises(ClientError, match="CLIENT="):
            locate(settings, base, None, None, cwd=base)


# --------------------------------------------------------------------- sync

class TestSync:
    def test_a_fix_on_main_reaches_the_client(self, settings, base):
        worktree = _new(settings, base)
        (base / "FIX.md").write_text("general fix\n")
        Git(base)("add", "FIX.md")
        Git(base)("commit", "-q", "-m", "fix: something general")

        assert sync_client(settings, base, worktree) == "rebased onto main"
        assert (worktree.path / "FIX.md").is_file()

    def test_uncommitted_work_blocks_a_sync(self, settings, base):
        worktree = _new(settings, base)
        (worktree.path / "README.md").write_text("edited\n")
        with pytest.raises(ClientError, match="uncommitted"):
            sync_client(settings, base, worktree)

    def test_a_conflict_is_aborted_cleanly(self, settings, base):
        worktree = _new(settings, base)
        for where, text in ((worktree.path, "client\n"), (base, "main\n")):
            (where / "README.md").write_text(text)
            Git(where)("commit", "-qam", f"edit README in {where.name}")
        before = Git(worktree.path)("rev-parse", "HEAD")

        with pytest.raises(ClientError, match="conflicts"):
            sync_client(settings, base, worktree)
        assert Git(worktree.path)("rev-parse", "HEAD") == before
        assert Git(worktree.path).dirty() == []


# ------------------------------------------------------------------ deliver

class TestDeliver:
    def test_the_whole_delivery(self, settings, base, tmp_path):
        worktree = _new(settings, base)
        spec = worktree.path / "surveys" / "job_stress_fa" / "survey.yaml"
        spec.write_text(spec.read_text() + "\n# tuned for the client\n")   # left uncommitted
        output = worktree.path / "output" / "job_stress_fa" / "answers"
        output.mkdir(parents=True)
        (output / "a.json").write_text("{}")
        (base / "FIX.md").write_text("fix\n")                              # main moved meanwhile
        Git(base)("add", "FIX.md")
        Git(base)("commit", "-q", "-m", "fix: general")

        result = deliver_client(settings, base, worktree, checks=False, push=False)

        git = Git(base)
        assert result.auto_committed == ["surveys/job_stress_fa/survey.yaml"]
        assert result.tag.startswith("delivered/acme/job_stress_fa/")
        assert git("rev-parse", "main") == git("rev-parse", f"{result.tag}^{{commit}}")
        assert (base / "surveys" / "job_stress_fa" / "survey.yaml").is_file()
        # linear history: init, fix, scaffold, final configuration
        assert git("rev-list", "--merges", "--count", "main") == "0"
        assert git("rev-list", "--count", "main") == "4"
        assert result.removed and not worktree.path.exists()
        assert not git.ok("show-ref", "--verify", "--quiet", "refs/heads/survey/job_stress_fa")
        with tarfile.open(result.archive) as bundle:
            assert "job_stress_fa/answers/a.json" in bundle.getnames()

    def test_uncommitted_code_outside_the_survey_stops_everything(self, settings, base):
        worktree = _new(settings, base)
        (worktree.path / "README.md").write_text("a general change\n")
        main_before = Git(base)("rev-parse", "main")

        with pytest.raises(ClientError, match="outside the survey folder"):
            deliver_client(settings, base, worktree, checks=False, push=False)
        assert Git(base)("rev-parse", "main") == main_before
        assert worktree.path.exists()

    def test_failed_checks_leave_nothing_tagged_or_merged(self, settings, base):
        worktree = _new(settings, base)
        (worktree.path / ".venv" / "bin").mkdir(parents=True)
        (worktree.path / ".venv" / "bin" / "python").write_text("")
        main_before = Git(base)("rev-parse", "main")

        with pytest.raises(ClientError, match="nothing was tagged"):
            deliver_client(settings, base, worktree, push=False, runner=lambda cmd, cwd: 1)
        assert Git(base)("rev-parse", "main") == main_before
        assert Git(base)("tag") == ""

    def test_a_second_delivery_on_the_same_day_gets_its_own_tag(self, settings, base):
        first = deliver_client(settings, base, _new(settings, base), checks=False, push=False)
        second = deliver_client(settings, base, _new(settings, base), checks=False, push=False)
        assert second.tag == f"{first.tag}-2"

    def test_keep_leaves_the_worktree(self, settings, base):
        worktree = _new(settings, base)
        result = deliver_client(settings, base, worktree, checks=False, push=False, keep=True)
        assert not result.removed and worktree.path.exists()

    def test_the_base_checkout_must_be_on_main(self, settings, base):
        worktree = _new(settings, base)
        Git(base)("switch", "-q", "-c", "elsewhere")
        with pytest.raises(ClientError, match="must be on main"):
            deliver_client(settings, base, worktree, checks=False, push=False)
