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

The second half of this module is the **account binding**: what a resolved account is once the
wave table carries it. A binding is two facts, not one — the configuration home the ticket's
account is identified, observed and attributed by, *and* whether that ticket's Claude processes
explicitly select that home or inherit the environment they were started in. A path alone cannot
say which: `~/.claude` written into `CLAUDE_CONFIG_DIR` and `CLAUDE_CONFIG_DIR` left alone reach
the same directory and are not the same login — the explicit spelling fails the keychain lookup
the inherited one succeeds at, which is how an account-less reviewer came to be told it was not
logged in (#110). `environment_delta` is the one place a binding becomes an environment, so no
consumer re-derives "inherit versus explicit" from a path, a name, or its own ambient environment.
"""

import collections
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


# --- the account binding ------------------------------------------------------------------

# The one variable Claude Code scopes a login to. Every Claude process of this project reaches it
# through this name rather than spelling it again, because the whole of "which account is this
# process on" is whether this variable is set here and to what.
CONFIG_HOME_VARIABLE = "CLAUDE_CONFIG_DIR"
# The two keys a wave-table row carries the binding under, and the two execution modes.
ACCOUNT_KEY = "account"
ACCOUNT_MODE_KEY = "account_mode"
INHERITED = "inherited"
EXPLICIT = "explicit"
ACCOUNT_MODES = (INHERITED, EXPLICIT)

# The resolved account of one ticket: the configuration home it is identified, observed and
# attributed by, and how its Claude processes select that home.
Binding = collections.namedtuple("Binding", ("directory", "mode"))


def inherited(directory):
    """The binding of a ticket that named no account: the caller's own login, left untouched.

    The directory is still carried, because a binding that carried none could not say which
    profile to read a child's transcript, cost or liveness out of. What inheriting decides is the
    *environment*, and that is `mode`'s job alone.
    """
    return Binding(str(directory), INHERITED)


def explicit(directory):
    """The binding of a ticket that named an account: that profile directory, selected by name."""
    return Binding(str(directory), EXPLICIT)


def row_binding(row):
    """The binding a wave-table row or launch record carries, or None where it carries no account.

    None is the row a release before accounts wrote, whose ticket runs wherever the reading
    process is pointed — the pre-account behavior, unchanged.

    A row carrying an account but no mode is the row the first account release wrote, and that
    release set `CLAUDE_CONFIG_DIR` for every row it wrote; reading it as explicit is therefore
    what that table meant, not a guess. New rows always carry both keys, so nothing written from
    here on relies on it.
    """
    directory = row.get(ACCOUNT_KEY)
    if not directory:
        return None
    mode = row.get(ACCOUNT_MODE_KEY)
    return Binding(str(directory), mode if mode in ACCOUNT_MODES else EXPLICIT)


def environment_delta(binding):
    """What this binding adds to a Claude process's environment: one variable, or nothing.

    The whole contract, and the only place it is decided. An inherited binding adds nothing —
    the process is started in exactly the environment its caller has, which is what keeps an
    account-less ticket on the login the operator is actually signed into. An explicit binding
    adds the one variable that moves a login, and nothing else.
    """
    if binding is None or binding.mode == INHERITED:
        return {}
    return {CONFIG_HOME_VARIABLE: str(binding.directory)}


def login_home(binding):
    """The configuration home this binding's *login* is asked at, or None for the caller's own.

    The path spelling of `environment_delta`, for the reads that are answered by a login rather
    than by a directory: a machine-wide answer one account's CLI gave belongs in that account's
    file, and an inherited binding's CLI answered for whichever login the reading process is on.

    Not where that ticket's *files* are. Those are at the directory the binding carries, in both
    modes: a child writes its session and transcript under the home it was launched with, and it
    goes on running there whatever login a later reader happens to be started under.
    """
    return environment_delta(binding).get(CONFIG_HOME_VARIABLE)


def process_environment(binding, base=None):
    """The environment to start a Claude process of this binding in, or None to inherit as-is.

    None rather than a copy of `os.environ` for an inherited binding, because None is what
    `subprocess` reads as "leave it alone" — a copy taken here and handed back would be a delta
    this module cannot see and cannot promise is empty.
    """
    delta = environment_delta(binding)
    if not delta:
        return None
    return dict(os.environ if base is None else base, **delta)
