"""The account registry: the one place a named account becomes a profile directory.

An **account** is a named Claude Code login. A ticket names one in its `## Routing` section, and
which subscription pays for that ticket's children is decided by the profile directory the name
resolves to — Claude Code scopes login state to `CLAUDE_CONFIG_DIR`, so the directory *is* the
account at exec time.

Two rules shape this module, both from
`docs/adr/0013-the-account-registry-is-not-profile-scoped.md`:

1. **The registry is machine-level and fixed, never profile-scoped.** Its default location is
   `~/.claude/agentcrew/accounts.toml` and it is deliberately *not* resolved through
   `CLAUDE_CONFIG_DIR`, which every other machine-level file of this project is. A registry whose
   whole job is to record the mapping *between* accounts cannot be stored per account: two copies
   that disagree route money to the wrong place, silently, which is the defect the account feature
   exists to remove.
2. **A name the registry does not hold is a hard failure.** Nothing here falls back to the
   coordinator's account, because a correct silent fallback and an incorrect one are the same code
   to every later reader.

The registry is TOML, one `[accounts]` table of `name = "<profile directory>"`:

    [accounts]
    work = "/Users/you/.claude"
    side = "/Users/you/.claude-side"

Its location is overridable by `AGENTCREW_ACCOUNT_REGISTRY`, a single-valued override that moves
the one registry rather than splitting it into several. An environment variable rather than a
command-line flag because a run is many processes — the driver, the dispatch renderer, the
dashboard, and every one a resume starts later — and each of them has to read the same registry
without a flag being threaded through each launch.

`profile_directory` is the one documented entry point from a name to a directory: no other module
reads the registry itself.
"""

import os
import pathlib
import tomllib

# The override, and the fixed default it moves: the operator's Claude configuration home by its
# own name under `~`, never the `CLAUDE_CONFIG_DIR` the rest of the project hangs its
# machine-level files off. `CONFIG_HOME` is that home's own name, which is also what a coordinator
# running under no `CLAUDE_CONFIG_DIR` is logged in under.
REGISTRY_OVERRIDE = "AGENTCREW_ACCOUNT_REGISTRY"
CONFIG_HOME = ".claude"
REGISTRY_RELATIVE = ("agentcrew", "accounts.toml")
# The one table the registry file carries.
REGISTRY_TABLE = "accounts"


class AccountsError(Exception):
    """The registry could not be read, or does not hold what was asked of it."""


class UnknownAccount(AccountsError):
    """That account name is not one the registry holds; carries the name and where it looked."""

    def __init__(self, name, registry):
        super().__init__(
            f"the account registry {registry} holds no account named `{name}` — register it there"
            f' as `{name} = "<profile directory>"` under [{REGISTRY_TABLE}], or route the ticket'
            " to an account it holds; the run stops rather than falling back to the coordinator's"
            " own account"
        )
        self.name = name
        self.registry = registry


def registry_path():
    """Where this machine's account registry lives: the override, or the fixed default.

    The override is refused unless it is absolute. A run is many processes started from many
    working directories, so a relative override would name a different file in each of them —
    which is the shadowing this registry is fixed and machine-level to prevent, arrived at by
    another road.
    """
    override = os.environ.get(REGISTRY_OVERRIDE)
    if not override:
        return pathlib.Path.home().joinpath(CONFIG_HOME, *REGISTRY_RELATIVE)
    path = pathlib.Path(override).expanduser()
    if not path.is_absolute():
        raise AccountsError(
            f"{REGISTRY_OVERRIDE} is `{override}`, which is not an absolute path — one registry"
            " serves the whole machine, and a relative path names a different file in every"
            " working directory a run's processes are started from"
        )
    return path


def load_registry(path=None):
    """Every account the registry names, as name → profile directory.

    A machine with no registry file has registered no account, which is not a fault: it is the
    machine of every operator who never asked for a second one, and its runs are single-account
    runs. A file that is there and cannot be read is a fault, because a run reading it is a run
    that was asked for an account.
    """
    path = pathlib.Path(path) if path else registry_path()
    if not path.exists():
        return {}
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AccountsError(f"the account registry {path} is unreadable: {error}") from error
    table = document.get(REGISTRY_TABLE)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise AccountsError(
            f"the account registry {path} carries no [{REGISTRY_TABLE}] table of"
            ' `name = "<profile directory>"` entries'
        )
    accounts = {}
    for name, directory in table.items():
        if not isinstance(directory, str) or not directory.strip():
            raise AccountsError(
                f"the account registry {path} maps `{name}` to {directory!r}, which is not a"
                " profile directory"
            )
        accounts[name] = str(pathlib.Path(directory.strip()).expanduser())
    return accounts


def profile_directory(name, registry=None):
    """The Claude Code profile directory that account name resolves to.

    The one entry point from a name to a directory. `registry` is the mapping already loaded,
    where a caller resolving several names reads the file once; left out, the file is read here.
    Raises `UnknownAccount` for a name the registry does not hold.
    """
    path = registry_path()
    accounts = load_registry(path) if registry is None else registry
    directory = accounts.get(name)
    if not directory:
        raise UnknownAccount(name, path)
    return directory
