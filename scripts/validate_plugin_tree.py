#!/usr/bin/env python3
"""Validate an AgentCrew plugin tree: manifest, skill slots, and the default config.

With `--config PATH`, validate one project config file instead of a tree: the same table shape,
with every cell optional, because a project file overrides the cells it names and inherits the rest.

Exits 0 when what was checked is sound, 1 when it is not, printing one line per problem.
"""

import argparse
import json
import os
import pathlib
import re
import sys
import tomllib

MANIFEST = ".claude-plugin/plugin.json"
MARKETPLACE = ".claude-plugin/marketplace.json"
DEFAULT_CONFIG = "config/agentcrew.default.toml"
SKILLS_DIR = "skills"
# Git's own storage, which a release export never carries and no check ever reads.
GIT_DIR = ".git"
# Reference material both skills point at, which belongs to neither of them.
REFERENCES_DIR = "references"

# The first release ships exactly these two skills.
SKILL_SLOTS = ("route", "crew")

# The rename cascade: the shipped surface speaks crew, never the skill's former name. The token is
# assembled from two halves so this checker's own source stays clean of it.
LEGACY_NAME = re.compile("orchestr" + "ate", re.IGNORECASE)
# These directories contain install-path-bearing plugin surfaces for the self-reference check.
# Text residue rules use `shipped_text_files`, which also excludes intentional test fixtures.
SCANNED_DIRS = (".claude-plugin", "config", REFERENCES_DIR, "scripts", SKILLS_DIR)

# A path a skill hands out relative to where that skill itself is installed: the skill-dir
# placeholder the coordinator resolves at run time, followed by the asset under it.
INSTALL_RELATIVE_PATH = re.compile(r"<([a-z][a-z0-9-]*)-skill-dir>/([\w./-]+)")
# The same, for an asset that sits outside every skill: the plugin-root placeholder, then the asset.
PLUGIN_RELATIVE_PATH = re.compile(r"<plugin-dir>/([\w./-]+)")
# A path that reaches into an installed skill or plugin by name instead.
INSTALL_PATH = re.compile(r"[\w.~$/-]*/(?:skills|plugins)/[\w./-]*[\w-]")

# These markers identify paths into the private bridge that was replaced by the vendored assets.
# Keep the old names split so this validator can scan its own source without finding its policy.
PRIVATE_SKILL_NAME = "codex" + "-implement"
PRIVATE_APP_NAME = "ChatGPT " + "Hands" + "-Free"
PRIVATE_BRIDGE_PATH = re.compile(
    r"(?:~|\$HOME|/(?:Users|home)/[^/\s]+)[^\n]*(?:"
    + re.escape(PRIVATE_SKILL_NAME)
    + "|"
    + re.escape(PRIVATE_APP_NAME)
    + r"|bridgectl)",
    re.IGNORECASE,
)
PRIVATE_ENV_PREFIX = "HANDS" + "_FREE_"
PRIVATE_ENV_TOKEN = re.compile(re.escape(PRIVATE_ENV_PREFIX) + r"[A-Z0-9_]+")
# Personal identifiers are data, never source: a maintainer's machine nicknames and account names
# are that maintainer's own, so the shipped tree names none of them. Tokens come from
# `AGENTCREW_LOCAL_IDENTIFIERS` (comma- or whitespace-separated), and failing that from
# `.agentcrew-local-identifiers` at the scanned root, one token per line with `#` starting a
# comment. Configure none and the rule is inert, which is the honest default for a tree that cannot
# know whose machines it is protecting.
LOCAL_IDENTIFIERS_ENV = "AGENTCREW_LOCAL_IDENTIFIERS"
LOCAL_IDENTIFIERS_FILE = ".agentcrew-local-identifiers"
SPEND_FIGURE = re.compile(
    r"(?:"
    r"[$€£¥]\s*(?:\d{2,}(?:\.\d+)?|\d+\.\d{2})(?![A-Za-z0-9_]|:)"
    r"|[$€£¥]\s*\d+(?:\.\d+)?\s*(?:/(?:month|mo|year)|per\s+(?:month|year)|"
    r"(?:USD|NZD|AUD|CAD|EUR|GBP|CNY|RMB)\b)"
    r"|\b\d+(?:\.\d+)?\s*(?:USD|NZD|AUD|CAD|EUR|GBP|CNY|RMB|dollars?|"
    r"/(?:month|mo|year)|per\s+(?:month|year))\b"
    r"|\b(?:total_cost|cost|price|spend|amount)(?:_[a-z]+)?\b[\"']?\s*[:=]\s*"
    r"\d+(?:\.\d+)?\b"
    r")",
    re.IGNORECASE,
)

# A markdown inline link's target: bare, or wrapped in angle brackets when it holds spaces, with
# an optional quoted title after it.
MARKDOWN_LINK = re.compile(r"""\]\(\s*(?:<([^>]*)>|([^)\s]+))(?:\s+["'(][^)]*)?\s*\)""")
EXTERNAL_LINK = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|#)")

# The implementer table: one case per cell, keyed workflow → cases.
IMPLEMENTER_CASES = {
    "tdd-refactor": ("core-complex", "core-routine", "non-core-complex", "non-core-routine"),
    "direct": ("any",),
    "spike": ("directed-collection", "open-exploration"),
    "ops": ("mechanical", "acceptance-judgement"),
    "acceptance": ("any",),
}
# The reviewer table: the same two axes that chose the implementer.
REVIEWER_CASES = ("core-complex", "core-routine", "non-core-complex", "non-core-routine")

CELL_FIELDS = ("executor", "model", "effort")
EXECUTORS = ("claude", "codex")
HOOK = "hooks.on-child-launch"

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
# The alias list the dispatch renderer refuses to launch on, so a cell this accepts is a cell that
# reaches a launch command intact rather than failing at the wave table.
SHAPES = "skills/crew/assets/dispatch/templates/shapes.toml"
# A bare vendor-less word is an alias whatever either vendor's catalogue holds today.
BARE_WORD = re.compile(r"^[A-Za-z]+$")
# A context-window suffix rides on both forms — `sonnet[1m]` is still that alias, and
# `claude-opus-5[1m]` is still that full ID — so it comes off before either test.
CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")


def model_aliases(root, problems):
    """Every alias the dispatch renderer rejects, lowercased, from the templates it renders with.

    The list comes from the tree under validation, so a cell is checked against the very renderer
    that will launch it. Failing to read it is a problem in its own right: an empty list would pass
    cells that renderer then refuses.
    """
    try:
        templates = tomllib.loads((root / SHAPES).read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        problems.append(f"{SHAPES}: the alias list the renderer enforces is unreadable: {error}")
        return frozenset()
    listed = templates.get("models", {}).get("aliases", [])
    return frozenset(alias.lower() for alias in listed if isinstance(alias, str))


def is_alias(model, aliases):
    """A model value that would reach a launch command as an alias instead of a full ID."""
    bare = CONTEXT_SUFFIX.sub("", model.strip())
    return bare.lower() in aliases or bool(BARE_WORD.match(bare))


def read_json(path):
    """The parsed document, or a problem string."""
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError) as error:
        return None, f"unreadable: {error}"


def check_manifest(root, problems):
    """The plugin manifest, or None when it could not be read."""
    manifest, problem = read_json(root / MANIFEST)
    if problem:
        problems.append(f"{MANIFEST}: {problem}")
        return None
    for field in ("name", "description", "version", "skills"):
        if not manifest.get(field):
            problems.append(f"{MANIFEST}: missing {field}")
    return manifest


def check_marketplace(root, manifest, problems):
    path = root / MARKETPLACE
    if not path.exists():
        return
    marketplace, problem = read_json(path)
    if problem:
        problems.append(f"{MARKETPLACE}: {problem}")
        return
    names = [entry.get("name") for entry in marketplace.get("plugins", [])]
    if manifest and manifest.get("name") not in names:
        problems.append(f"{MARKETPLACE}: lists {names}, not the plugin {manifest.get('name')!r}")


def read_frontmatter(path):
    """The `key: value` pairs of a SKILL.md frontmatter block, or a problem string."""
    try:
        text = path.read_text()
    except OSError as error:
        return None, f"unreadable: {error}"
    if not text.startswith("---\n"):
        return None, "no frontmatter block"
    _, _, rest = text.partition("---\n")
    block, marker, _ = rest.partition("\n---")
    if not marker:
        return None, "unterminated frontmatter block"
    fields = {}
    for line in block.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields, None


def check_links(root, path, problems):
    """Every relative link in a SKILL.md resolves from where the skill is installed."""
    relative = path.relative_to(root)
    for bracketed, bare in MARKDOWN_LINK.findall(path.read_text()):
        target = bracketed or bare
        if not target or EXTERNAL_LINK.match(target):
            continue
        if not (path.parent / target.split("#")[0]).exists():
            problems.append(f"{relative}: link {target} resolves to nothing")


def check_skills(root, manifest, problems):
    entries = (manifest or {}).get("skills", [])
    listed = set()
    for entry in entries:
        path = (root / entry).resolve()
        if path in listed:
            problems.append(f"{entry}: listed twice in {MANIFEST}")
        listed.add(path)
        if not (path / "SKILL.md").is_file():
            problems.append(f"{entry}: listed in the manifest without a SKILL.md")
            continue
        fields, problem = read_frontmatter(path / "SKILL.md")
        if problem:
            problems.append(f"{entry}/SKILL.md: {problem}")
            continue
        if not fields.get("description"):
            problems.append(f"{entry}/SKILL.md: missing description")
        name = fields.get("name")
        if name != path.name:
            problems.append(f"{entry}/SKILL.md: name {name!r} disagrees with its directory")
        if name not in SKILL_SLOTS:
            problems.append(f"{entry}: not one of this plugin's skill slots {SKILL_SLOTS}")
        check_links(root, path / "SKILL.md", problems)

    for slot in SKILL_SLOTS:
        if (root / SKILLS_DIR / slot).resolve() not in listed:
            problems.append(f"{SKILLS_DIR}/{slot}: skill slot missing from {MANIFEST}")

    skills_dir = root / SKILLS_DIR
    for path in sorted(p for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else []:
        if path.resolve() not in listed:
            problems.append(f"{SKILLS_DIR}/{path.name}: on disk but absent from {MANIFEST}")


def check_cell(config, key, label, complete, aliases, problems):
    """One vendor/model/effort cell of a model table."""
    cell = config
    for part in key.split("."):
        cell = cell.get(part) if isinstance(cell, dict) else None
        if cell is None:
            if complete:
                problems.append(f"{label}: missing [{key}]")
            return
    for field in CELL_FIELDS:
        value = cell.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{label}: [{key}] needs a non-empty {field}")
    if isinstance(cell.get("executor"), str) and cell["executor"] not in EXECUTORS:
        problems.append(
            f"{label}: [{key}] executor {cell['executor']!r} is not one of {EXECUTORS}"
        )
    model = cell.get("model")
    if isinstance(model, str) and model.strip() and is_alias(model, aliases):
        problems.append(f"{label}: [{key}] model {model!r} is an alias, not a full model ID")
    for field in sorted(set(cell) - set(CELL_FIELDS)):
        problems.append(f"{label}: [{key}] carries an unknown field {field!r}")


def check_config(root, problems):
    path = root / DEFAULT_CONFIG
    try:
        text = path.read_text()
    except FileNotFoundError:
        problems.append(f"{DEFAULT_CONFIG}: missing")
        return
    except OSError as error:
        problems.append(f"{DEFAULT_CONFIG}: unreadable: {error}")
        return
    check_config_text(text, DEFAULT_CONFIG, True, model_aliases(root, problems), problems)


def check_config_text(text, label, complete, aliases, problems):
    """The config in `text`: every cell answered when `complete`, else only the cells it overrides.

    The shipped defaults answer every case, which is what lets a project file inherit the cells it
    leaves out.
    """
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        problems.append(f"{label}: unparsable: {error}")
        return

    expected = {"implementer", "reviewer", "hooks"}
    for section in sorted(set(config) - expected):
        problems.append(f"{label}: unknown section [{section}]")

    implementer = config.get("implementer", {})
    for workflow in sorted(set(implementer) - set(IMPLEMENTER_CASES)):
        problems.append(f"{label}: unknown workflow [implementer.{workflow}]")
    for workflow, cases in IMPLEMENTER_CASES.items():
        for case in sorted(set(implementer.get(workflow, {})) - set(cases)):
            problems.append(f"{label}: unknown case [implementer.{workflow}.{case}]")
        for case in cases:
            check_cell(config, f"implementer.{workflow}.{case}", label, complete, aliases, problems)

    for case in sorted(set(config.get("reviewer", {})) - set(REVIEWER_CASES)):
        problems.append(f"{label}: unknown case [reviewer.{case}]")
    for case in REVIEWER_CASES:
        check_cell(config, f"reviewer.{case}", label, complete, aliases, problems)

    check_hook(config, label, complete, problems)


def check_hook(config, label, complete, problems):
    hooks = config.get("hooks", {})
    hook = hooks.get("on-child-launch") if isinstance(hooks, dict) else None
    if hook is None:
        if complete:
            problems.append(f"{label}: missing [{HOOK}]")
        return
    if not isinstance(hook, dict):
        problems.append(f"{label}: [{HOOK}] must be a table with a command and an env")
        return
    if not isinstance(hook.get("command"), str):
        problems.append(f"{label}: [{HOOK}] needs a command, empty string when unused")
    env = hook.get("env", {})
    if isinstance(env, dict):
        # The hook's env reaches the child as process environment, which carries strings only.
        for name, value in env.items():
            if not isinstance(value, str):
                problems.append(f"{label}: [{HOOK}.env] {name} must be quoted as a string")
    else:
        problems.append(f"{label}: [{HOOK}.env] must be a table of environment variables")
    for field in sorted(set(hook) - {"command", "env"}):
        problems.append(f"{label}: [{HOOK}] carries an unknown field {field!r}")


def validate_project_config(path):
    """Every problem in one project config file, whose cells are all optional overrides."""
    problems = []
    label = path.name
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [f"{label}: missing"]
    except OSError as error:
        return [f"{label}: unreadable: {error}"]
    check_config_text(text, label, False, model_aliases(PLUGIN_ROOT, problems), problems)
    return problems


def local_identifiers(root):
    """The personal identifiers this run rejects, read from the environment or the scanned root."""
    configured = os.environ.get(LOCAL_IDENTIFIERS_ENV)
    if configured is None:
        try:
            lines = (root / LOCAL_IDENTIFIERS_FILE).read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            return ()
        configured = " ".join(line.split("#", 1)[0] for line in lines)
    return tuple(token for token in re.split(r"[,\s]+", configured) if token)


def local_identifier_pattern(identifiers):
    """Match any configured identifier as a whole word; match nothing when none are configured."""
    if not identifiers:
        return None
    return re.compile(
        r"(?<![A-Za-z0-9_])(?:"
        + "|".join(re.escape(identifier) for identifier in identifiers)
        + r")(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


def shipped_paths(root):
    """Every shipped file under `root`, as a relative path.

    A release is a `git ls-files` export, so `.git/` is never published and never scanned: its
    reflogs carry the committer's name, which would fail every clone whose maintainer configured
    their own git identity as a local identifier.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if GIT_DIR in relative.parts:
            continue
        yield relative


def shipped_text_files(root):
    """Every readable shipped text file, excluding only the validator's top-level fixtures."""
    for relative in shipped_paths(root):
        path = root / relative
        if relative.parts and relative.parts[0].lower() == "tests":
            continue
        # The identifier list names the very tokens it bans, so it is policy, not residue.
        if relative.name == LOCAL_IDENTIFIERS_FILE:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        yield relative, text


RESIDUE_RULES = (
    (PRIVATE_BRIDGE_PATH, "{match} — private bridge path is not public"),
    (PRIVATE_ENV_TOKEN, "{match} — private environment token is not public"),
    (SPEND_FIGURE, "{match} — spend figure is not public"),
    (LEGACY_NAME, "{match} — the rename cascade says crew"),
)


def residue_rules(root):
    """The fixed rules, plus the personal-identifier rule when this run has identifiers to reject."""
    pattern = local_identifier_pattern(local_identifiers(root))
    if pattern is None:
        return RESIDUE_RULES
    return RESIDUE_RULES + ((pattern, "{match} — personal identifier is not public"),)


def check_personal_residue(root, problems):
    """Reject personal residue in one pass over every shipped, non-test text file."""
    rules = residue_rules(root)
    for relative, text in shipped_text_files(root):
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, message in rules:
                found = pattern.search(line)
                if found:
                    problems.append(
                        f"{relative}:{number}: {message.format(match=found.group())}"
                    )


def shipped_files(root):
    """Every file this plugin ships, keyed by name, as relative paths."""
    shipped = {}
    for relative in shipped_paths(root):
        shipped.setdefault(relative.name, []).append(relative)
    return shipped


def check_self_reference(root, problems):
    """A skill reaches its own assets from where it is installed, never from a fixed path."""
    shipped = shipped_files(root)
    for directory in SCANNED_DIRS:
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue  # not text, so it names no paths to check
            relative = path.relative_to(root)
            for number, line in enumerate(text.splitlines(), start=1):
                for slot, target in INSTALL_RELATIVE_PATH.findall(line):
                    if slot not in SKILL_SLOTS:
                        problems.append(
                            f"{relative}:{number}: <{slot}-skill-dir> names no skill of this plugin"
                        )
                    elif not (root / SKILLS_DIR / slot / target).exists():
                        problems.append(
                            f"{relative}:{number}: {target} is absent from {SKILLS_DIR}/{slot}"
                        )
                for target in PLUGIN_RELATIVE_PATH.findall(line):
                    if not (root / target).exists():
                        problems.append(
                            f"{relative}:{number}: {target} is absent from the plugin root"
                        )
                for found in INSTALL_PATH.findall(line):
                    name = found.rsplit("/", 1)[-1]
                    if name in shipped:
                        problems.append(
                            f"{relative}:{number}: {found} hard-codes an install path to "
                            f"{shipped[name][0]} — name it from the skill's own directory"
                        )


def validate(root):
    """Every problem found in the tree at `root`, in the order the checks run."""
    problems = []
    manifest = check_manifest(root, problems)
    check_marketplace(root, manifest, problems)
    check_skills(root, manifest, problems)
    check_config(root, problems)
    check_personal_residue(root, problems)
    check_self_reference(root, problems)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=PLUGIN_ROOT,
        type=pathlib.Path,
        help="plugin tree to validate (default: the tree this script ships in)",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        help="validate this project config file instead of a plugin tree",
    )
    args = parser.parse_args(argv)

    if args.config:
        target = args.config
        problems = validate_project_config(target)
        headline = f"project config OK: {target}"
    else:
        target = args.root.resolve()
        problems = validate(target)
        headline = f"plugin tree OK: {target}"

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} problem(s) in {target}", file=sys.stderr)
        return 1
    print(headline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
