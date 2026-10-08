"""Client worktrees — several clients at once, each isolated, one repository.

A client's questionnaire is worked on in its own ``git worktree`` on a
short-lived branch ``survey/<id>``, under ``clients/<client>/<id>/`` next to the
base checkout. Each worktree has its own ``output/``, ``.env`` and ``.venv``, so
two clients never share a queue, a configuration or — through an editable
install pointing at the wrong tree — code. Delivery tags the result, rebases it
onto ``main``, fast-forwards ``main``, pushes, and removes the worktree.

This is what the fifteen 2.x repository copies were trying to be, without the
copies drifting apart: general fixes go to ``main`` and reach every client
worktree through ``sync``.

All git work goes through :class:`Git`, a thin subprocess wrapper, so the module
is tested against real repositories in a temporary directory.
"""

from __future__ import annotations

import os
import re
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from personabot.errors import PersonaBotError
from personabot.logging_setup import get_logger
from personabot.settings import Settings

log = get_logger(__name__)

BASE_BRANCH = "main"
BRANCH_PREFIX = "survey/"
TAG_PREFIX = "delivered"
ARCHIVE_DIR = "_archive"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
_KEY_LINE = re.compile(r'^\s*GEMINI_API_KEY_(\d+)\s*=\s*"?([^"\n#]*)"?', re.MULTILINE)
# Survey selection and form URLs are per worktree, never inherited.
_DROP_LINE = re.compile(r"^\s*(GEMINI_API_KEY_\d+|SURVEY|SURVEY_CONFIG|BASE_FORM_URL)\s*=")

Runner = Callable[[Sequence[str], Path], int]


class ClientError(PersonaBotError):
    """A client-worktree operation was refused or could not complete."""


# ================================================================== git

class Git:
    """Runs git in one directory and fails with git's own message."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = Path(cwd)

    def __call__(self, *args: str, check: bool = True, strip: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=self.cwd, capture_output=True, text=True,
        )
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise ClientError(f"git {' '.join(args)} failed in {self.cwd}:\n{detail}")
        return completed.stdout.strip() if strip else completed.stdout

    def ok(self, *args: str) -> bool:
        return subprocess.run(
            ["git", *args], cwd=self.cwd, capture_output=True, text=True,
        ).returncode == 0

    def has_remote(self, name: str = "origin") -> bool:
        return name in self("remote").split()

    def dirty(self, *, untracked: bool = True) -> list[str]:
        """Paths with uncommitted changes (ignored files never count)."""
        mode = "--untracked-files=all" if untracked else "--untracked-files=no"
        # Not stripped: porcelain lines start with a two-column status that
        # may begin with a space (" M path").
        lines = self("status", "--porcelain", mode, strip=False).splitlines()
        return [line[3:].strip().strip('"') for line in lines if line.strip()]


def _default_runner(command: Sequence[str], cwd: Path) -> int:
    return subprocess.run(list(command), cwd=cwd).returncode


# ============================================================ locations

def base_checkout(start: Path | None = None) -> Path:
    """The main worktree of the repository containing ``start``.

    Works from inside a client worktree too: every worktree shares one
    ``.git`` directory, whose parent is the base checkout.
    """
    common = Git(Path(start or Path.cwd()))("rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent


def clients_root(settings: Settings, base: Path) -> Path:
    return Path(settings.clients_dir) if settings.clients_dir else base.parent / "clients"


@dataclass(frozen=True, slots=True)
class ClientWorktree:
    client: str
    survey: str
    path: Path

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.survey}"

    @property
    def label(self) -> str:
        return f"{self.client}/{self.survey}"


def _validate_name(value: str, what: str) -> str:
    if not _NAME.match(value or ""):
        raise ClientError(
            f"{what} {value!r} is not usable: letters, digits, `_` and `-`, "
            f"starting with a letter or digit."
        )
    return value


def locate(settings: Settings, base: Path, client: str | None, survey: str | None,
           *, cwd: Path | None = None) -> ClientWorktree:
    """The client worktree named, or the one ``cwd`` is inside."""
    root = clients_root(settings, base)
    if client and survey:
        return ClientWorktree(_validate_name(client, "Client"), _validate_name(survey, "Survey"),
                              root / client / survey)
    here = Path(cwd or Path.cwd()).resolve()
    try:
        relative = here.relative_to(root.resolve())
    except ValueError:
        raise ClientError(
            "Name the client and survey (e.g. `make deliver CLIENT=acme SURVEY=job_stress_fa`), "
            f"or run this from inside a client worktree under {root}."
        ) from None
    if len(relative.parts) < 2 or relative.parts[0] == ARCHIVE_DIR:
        raise ClientError(f"{here} is not inside a client worktree ({root}/<client>/<survey>).")
    client, survey = relative.parts[0], relative.parts[1]
    return ClientWorktree(client, survey, root / client / survey)


# ================================================================== .env

def parse_key_selection(spec: str | None, available: int) -> list[int]:
    """``"1-7,9"`` → ``[1, 2, …, 7, 9]``. ``None``/``"all"`` → every key."""
    if not spec or spec.strip().lower() == "all":
        return list(range(1, available + 1))
    chosen: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ClientError(f"Bad key selection {part!r}; use e.g. `1-7,9`.")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if low < 1 or high > available or low > high:
            raise ClientError(
                f"Key selection {part!r} is outside 1-{available} "
                f"(the base .env has {available} key(s))."
            )
        chosen.extend(i for i in range(low, high + 1) if i not in chosen)
    if not chosen:
        raise ClientError("The key selection is empty.")
    return chosen


def base_keys(env_text: str) -> list[str]:
    """The base ``.env``'s Gemini keys, ordered by their number."""
    found = {int(n): value.strip() for n, value in _KEY_LINE.findall(env_text) if value.strip()}
    return [found[n] for n in sorted(found)]


def render_client_env(env_text: str, *, worktree: ClientWorktree, keys: Sequence[str]) -> str:
    """The base ``.env`` with this client's survey and key subset.

    Keys are renumbered from 1. Survey selection and form URLs are dropped
    from the inherited text: in a client worktree ``SURVEY`` is always that
    worktree's survey, and a URL belongs in the survey's ``form.url``.
    """
    kept = [line for line in env_text.splitlines() if not _DROP_LINE.match(line)]
    header = [
        "# =============================================================================",
        f"# Client worktree {worktree.label} — generated by `personabot client new`",
        f"# from the base checkout's .env on {datetime.now():%Y-%m-%d}.",
        "# =============================================================================",
        f'SURVEY="{worktree.survey}"',
        *(f'GEMINI_API_KEY_{i}="{key}"' for i, key in enumerate(keys, 1)),
        "",
    ]
    return "\n".join(header + kept).rstrip() + "\n"


def _write_private(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


# ================================================================== new

def create_client(
    settings: Settings,
    base: Path,
    client: str,
    survey: str,
    *,
    source: str | None = None,
    keys: str | None = None,
    install: bool = True,
    runner: Runner = _default_runner,
) -> ClientWorktree:
    """Create an isolated worktree for one client questionnaire."""
    from personabot.application.scaffold import new_survey
    from personabot.workspace import SPEC_FILENAME

    worktree = ClientWorktree(
        _validate_name(client, "Client"),
        _validate_name(survey, "Survey"),
        clients_root(settings, base) / client / survey,
    )
    git = Git(base)

    if worktree.path.exists():
        raise ClientError(f"{worktree.path} already exists.")
    if git.ok("show-ref", "--verify", "--quiet", f"refs/heads/{worktree.branch}"):
        raise ClientError(
            f"Branch {worktree.branch} already exists — another client is working on "
            f"survey {survey!r}. Deliver or remove it first (`make clients`)."
        )

    _refresh_main(git)
    git("worktree", "add", "-b", worktree.branch, str(worktree.path), BASE_BRANCH)
    log.info("Created worktree %s on %s.", worktree.path, worktree.branch)

    # --- .env ---
    base_env = base / ".env"
    if base_env.is_file():
        text = base_env.read_text(encoding="utf-8")
        available = base_keys(text)
        selected = [available[i - 1] for i in parse_key_selection(keys, len(available))] if available else []
        _write_private(worktree.path / ".env", render_client_env(text, worktree=worktree, keys=selected))
        log.info("Wrote %s/.env with %d key(s).", worktree.path, len(selected))
    else:
        example = (worktree.path / ".env.example").read_text(encoding="utf-8")
        _write_private(worktree.path / ".env", render_client_env(example, worktree=worktree, keys=[]))
        log.warning("The base checkout has no .env; the client .env has no API keys yet.")

    # --- the survey ---
    survey_dir = worktree.path / "surveys" / survey
    if (survey_dir / SPEC_FILENAME).is_file():
        log.info("Survey %s is already on %s; continuing it.", survey, BASE_BRANCH)
    else:
        local = Settings(_env_file=None, surveys_dir=worktree.path / "surveys")
        new_survey(local, survey, source=source)
        worktree_git = Git(worktree.path)
        worktree_git("add", "--", f"surveys/{survey}")
        worktree_git("commit", "-q", "-m", f"survey({survey}): start for client {client}")
        log.info("Scaffolded surveys/%s%s and committed it.", survey,
                 f" from {source}" if source else "")

    # --- the venv ---
    if install:
        log.info("Installing the worktree's own virtualenv (make install)…")
        if runner(["make", "install"], worktree.path) != 0:
            raise ClientError(
                f"`make install` failed in {worktree.path}. The worktree is ready "
                f"otherwise; fix the problem and run `make install` there."
            )
    return worktree


def _refresh_main(git: Git) -> None:
    """Fast-forward the base checkout's main from origin, when that is safe."""
    if not git.has_remote():
        return
    if git("rev-parse", "--abbrev-ref", "HEAD") != BASE_BRANCH:
        log.warning("The base checkout is not on %s; using the local %s as is.",
                    BASE_BRANCH, BASE_BRANCH)
        return
    if not git.ok("pull", "--ff-only", "--quiet", "origin", BASE_BRANCH):
        log.warning("Could not fast-forward %s from origin (offline, or local commits); "
                    "using the local %s.", BASE_BRANCH, BASE_BRANCH)


# ================================================================== list

@dataclass(slots=True)
class ClientStatus:
    worktree: ClientWorktree
    ahead: int = 0
    behind: int = 0
    dirty: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    venv: bool = False
    pending_answers: int = 0
    submitted: int = 0


def list_clients(settings: Settings, base: Path) -> list[ClientStatus]:
    root = clients_root(settings, base).resolve()
    statuses: list[ClientStatus] = []
    for path, branch in _worktrees(Git(base)):
        try:
            relative = Path(path).resolve().relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) != 2 or not branch.startswith(BRANCH_PREFIX):
            continue
        worktree = ClientWorktree(relative.parts[0], relative.parts[1], Path(path))
        git = Git(worktree.path)
        behind, ahead = (int(n) for n in git(
            "rev-list", "--left-right", "--count", f"{BASE_BRANCH}...HEAD"
        ).split())
        env = worktree.path / ".env"
        answers = worktree.path / "output" / worktree.survey / "answers"
        statuses.append(ClientStatus(
            worktree=worktree,
            ahead=ahead,
            behind=behind,
            dirty=git.dirty(),
            keys=base_keys(env.read_text(encoding="utf-8")) if env.is_file() else [],
            venv=(worktree.path / ".venv" / "bin" / "python").exists(),
            pending_answers=len(list(answers.glob("*.json"))) if answers.is_dir() else 0,
            submitted=len(list((answers / "done").glob("*.json"))) if answers.is_dir() else 0,
        ))
    return statuses


def render_clients(statuses: list[ClientStatus]) -> str:
    if not statuses:
        return "No client worktrees. Start one with `make client CLIENT=<name> SURVEY=<id>`."
    lines = [f"{'client/survey':42s} {'ahead':>5s} {'behind':>6s} {'dirty':>5s} "
             f"{'keys':>4s} {'pending':>7s} {'sent':>5s}  venv"]
    for s in statuses:
        lines.append(
            f"{s.worktree.label:42s} {s.ahead:5d} {s.behind:6d} {len(s.dirty):5d} "
            f"{len(s.keys):4d} {s.pending_answers:7d} {s.submitted:5d}  "
            f"{'yes' if s.venv else 'NO — run make install'}"
        )
    shared = _key_overlaps(statuses)
    if shared:
        lines.append("")
        for (a, b), count in shared.items():
            lines.append(
                f"! {a} and {b} share {count} key(s): generating for both at once "
                f"hits quota sooner. Split them with KEYS=… on `make client`."
            )
    behind = [s.worktree.label for s in statuses if s.behind]
    if behind:
        lines.append("")
        lines.append(f"Behind {BASE_BRANCH}: {', '.join(behind)} — `make sync` inside each.")
    return "\n".join(lines)


def _key_overlaps(statuses: list[ClientStatus]) -> dict[tuple[str, str], int]:
    overlaps: dict[tuple[str, str], int] = {}
    for i, first in enumerate(statuses):
        for second in statuses[i + 1:]:
            count = len(set(first.keys) & set(second.keys))
            if count:
                overlaps[(first.worktree.label, second.worktree.label)] = count
    return overlaps


def _worktrees(git: Git) -> list[tuple[str, str]]:
    entries, path, branch = [], None, ""
    for line in git("worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.startswith("branch "):
            branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
        elif not line and path:
            entries.append((path, branch))
            path, branch = None, ""
    return entries


# ================================================================== sync

def sync_client(settings: Settings, base: Path, worktree: ClientWorktree) -> str:
    """Bring a client branch up to date with main by rebasing onto it."""
    git = _require_worktree(worktree)
    dirty = git.dirty(untracked=False)
    if dirty:
        raise ClientError(
            f"{worktree.label} has uncommitted changes ({', '.join(dirty[:5])}). "
            f"Commit them, then sync."
        )
    _refresh_main(Git(base))
    before = git("rev-parse", "HEAD")
    if not git.ok("rebase", BASE_BRANCH):
        git("rebase", "--abort", check=False)
        raise ClientError(
            f"Rebasing {worktree.branch} onto {BASE_BRANCH} conflicts. Nothing was "
            f"changed. Resolve it by hand in {worktree.path}: `git rebase {BASE_BRANCH}`."
        )
    after = git("rev-parse", "HEAD")
    return "already up to date" if before == after else f"rebased onto {BASE_BRANCH}"


def _require_worktree(worktree: ClientWorktree) -> Git:
    if not (worktree.path / ".git").exists():
        raise ClientError(f"No client worktree at {worktree.path} (`make clients` lists them).")
    git = Git(worktree.path)
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    if current != worktree.branch:
        raise ClientError(f"{worktree.path} is on {current}, expected {worktree.branch}.")
    return git


# =============================================================== deliver

@dataclass(slots=True)
class Delivery:
    worktree: ClientWorktree
    tag: str = ""
    archive: Path | None = None
    pushed: bool = False
    removed: bool = False
    auto_committed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Delivered {self.worktree.label}", f"  tag      : {self.tag}"]
        if self.auto_committed:
            lines.append(f"  committed: {len(self.auto_committed)} survey file(s) left uncommitted")
        lines.append(f"  main     : fast-forwarded{' and pushed' if self.pushed else ' (not pushed)'}")
        lines.append(f"  archive  : {self.archive or '(no output to archive)'}")
        lines.append(f"  worktree : {'removed' if self.removed else f'kept at {self.worktree.path}'}")
        lines += [f"  ! {note}" for note in self.notes]
        return "\n".join(lines)


def deliver_client(
    settings: Settings,
    base: Path,
    worktree: ClientWorktree,
    *,
    push: bool = True,
    keep: bool = False,
    checks: bool = True,
    archive: bool = True,
    runner: Runner = _default_runner,
) -> Delivery:
    """Tag, merge into main, push and clean up one client worktree.

    Ordered so that a failure leaves nothing half-done: every refusal and
    check happens before the first tag or merge.
    """
    result = Delivery(worktree=worktree)
    git = _require_worktree(worktree)
    base_git = Git(base)

    if base_git("rev-parse", "--abbrev-ref", "HEAD") != BASE_BRANCH:
        raise ClientError(f"The base checkout {base} must be on {BASE_BRANCH} to deliver.")
    if base_git.dirty(untracked=False):
        raise ClientError(f"The base checkout {base} has uncommitted changes; commit or stash them.")

    # 1. survey files are committed for you; anything else is a decision.
    pending = git.dirty()
    survey_prefix = f"surveys/{worktree.survey}/"
    others = [p for p in pending if not p.startswith(survey_prefix)]
    if others:
        raise ClientError(
            "Uncommitted changes outside the survey folder:\n  "
            + "\n  ".join(others[:15])
            + "\nGeneral code changes need their own commit (or `git stash`) before delivery."
        )
    if pending:
        git("add", "--", survey_prefix)
        git("commit", "-q", "-m", f"survey({worktree.survey}): final configuration at delivery")
        result.auto_committed = pending

    # 2. on top of the current main
    sync_client(settings, base, worktree)

    # 3. checks, in the worktree's own environment
    if checks:
        python = worktree.path / ".venv" / "bin" / "python"
        if not python.exists():
            raise ClientError(f"{worktree.path} has no .venv; run `make install` there first.")
        for command, what in (
            ([str(python), "-m", "personabot.cli", "--survey", worktree.survey, "validate-config"],
             "validate-config"),
            ([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"], "the test suite"),
        ):
            if runner(command, worktree.path) != 0:
                raise ClientError(f"{what} failed in {worktree.path}; nothing was tagged or merged.")

    # 4. the output, before the worktree that holds it goes away
    output = worktree.path / "output" / worktree.survey
    if archive and output.is_dir() and any(output.rglob("*")):
        target_dir = clients_root(settings, base) / ARCHIVE_DIR / worktree.client
        target_dir.mkdir(parents=True, exist_ok=True)
        result.archive = target_dir / f"{worktree.survey}-{datetime.now():%Y%m%dT%H%M%S}.tar.gz"
        with tarfile.open(result.archive, "w:gz") as bundle:
            bundle.add(output, arcname=f"{worktree.survey}")

    # 5. tag the exact commit that lands on main
    result.tag = _free_tag(git, f"{TAG_PREFIX}/{worktree.client}/{worktree.survey}/{datetime.now():%Y-%m-%d}")
    git("tag", "-a", result.tag, "-m", f"Delivered {worktree.survey} to {worktree.client}")

    # 6. main
    base_git("merge", "--ff-only", "--quiet", worktree.branch)

    # 7. remote
    if push and base_git.has_remote():
        if base_git.ok("push", "--quiet", "origin", BASE_BRANCH, result.tag):
            result.pushed = True
        else:
            result.notes.append(
                f"Push failed; main and the tag are safe locally. Retry: "
                f"git push origin {BASE_BRANCH} {result.tag}"
            )

    # 8. clean up
    if not keep:
        base_git("worktree", "remove", "--force", str(worktree.path))
        base_git("branch", "-d", worktree.branch)
        client_dir = worktree.path.parent
        if client_dir.is_dir() and not any(client_dir.iterdir()):
            client_dir.rmdir()
        result.removed = True
    return result


def _free_tag(git: Git, wanted: str) -> str:
    candidate, n = wanted, 1
    while git.ok("rev-parse", "--verify", "--quiet", f"refs/tags/{candidate}"):
        n += 1
        candidate = f"{wanted}-{n}"
    return candidate


__all__ = [
    "ClientError", "ClientWorktree", "Delivery", "base_checkout", "clients_root",
    "create_client", "deliver_client", "list_clients", "locate", "render_clients",
    "sync_client",
]
