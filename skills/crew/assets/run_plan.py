"""Build, validate, persist, and query the immutable meaning of a Wave table."""

from dataclasses import dataclass, replace
import json
import pathlib
import re
import tomllib

import accounts


TICKET_FILE = re.compile(r"^(\d+)(?:-.*)?\.md$")
SECTION = re.compile(r"^##\s+(.*?)\s*$")
ROUTING_LINE = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$")
BLOCKER = re.compile(r"#(\d+)")
ROUTING_SECTION = "routing"
BLOCKED_BY_SECTION = "blocked by"
TEMPLATES = pathlib.Path(__file__).resolve().parent / "dispatch" / "templates" / "shapes.toml"
DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parents[3] / "config" / "agentcrew.default.toml"
PROJECT_CONFIG_NAME = "agentcrew.toml"
CREW_STATE_DIR_NAME = ".crew"
EXECUTORS = ("claude", "codex")
# The witness launcher is the Claude CLI; exposing that here makes its complete route available to
# every consumer without making presentation code infer a vendor from a model name.
WITNESS_EXECUTOR = "claude"
REVIEW_VENDORS = EXECUTORS
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
ACCOUNT_MODES = accounts.ACCOUNT_MODES
TRACKERS = ("github", "local")
# The tracker whose Run-directory ticket file is a stub rather than the ticket itself.
GITHUB_TRACKER = TRACKERS[0]
# What a queued ticket's finding still leaves open, which is what its child diagnoses first
# (ADR-0028). A finding whose cause and change site are both known is an edit, never a queued
# ticket, so there is no fourth word.
OPEN_WORDS = ("cause", "approach", "reach")
# The routing a queued ticket is opened on: one cell of the crew config file, the operator's
# standing approval. `account` is deliberately not one of these keys — which subscription pays is
# not a fact about the kind of work, so the cell never concludes one.
QUEUED_SECTION = "queued"
QUEUED_FIELDS = ("workflow", "executor", "model", "effort")
QUEUED_REVIEW = "review"
QUEUED_ACCOUNT = "account"
BARE_WORD = re.compile(r"^[A-Za-z]+$")
CONTEXT_SUFFIX = re.compile(r"\[[^\]]*\]$")


class RunPlanError(Exception):
    """One or more deterministic problems with a run plan."""

    def __init__(self, problems):
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class CodexConfig:
    bridge: str
    state_dir: str


@dataclass(frozen=True)
class LaunchHook:
    command: str | None
    env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RunMetadata:
    repo_root: str
    crew_worktree: str
    spec_path: str
    integration_branch: str
    integration_base_commit: str
    coordinator_name: str
    coordinator_pid: int
    crew_skill_dir: str
    tmux_session: str
    permission_mode: str
    coordinator_config_home: str
    coordinator_session: str = ""
    coordinator_address: str = ""
    base_branch: str | None = None
    feature_dir: str | None = None
    repair_model: str | None = None
    witness_model: str | None = None
    witness_budget_usd: float | None = None
    tracker: str | None = None
    declared_accounts: tuple[str, ...] = ()
    codex: CodexConfig | None = None
    launch_hook: LaunchHook | None = None


@dataclass(frozen=True)
class ReviewLane:
    vendor: str
    model: str
    effort: str


@dataclass(frozen=True)
class Queued:
    """Why one ticket was queued into this Run: the finding's source, and what it leaves open."""

    source: str
    open: str


@dataclass(frozen=True)
class QueuedRouting:
    """The routing a queued ticket is opened on, resolved from the `[queued]` config cell."""

    workflow: str
    executor: str
    model: str
    effort: str
    review: ReviewLane | None = None


@dataclass(frozen=True)
class PlannedTicket:
    id: str
    title: str
    path: str
    workflow: str
    executor: str
    model: str
    effort: str
    binding: accounts.Binding
    blocked_by: tuple[str, ...] = ()
    review: ReviewLane | None = None
    slug: str | None = None
    base_commit: str | None = None
    # Absent on an ordinary ticket; present on one this Run queued into itself, which is what
    # selects the diagnosing child at dispatch (ADR-0028).
    queued: Queued | None = None


@dataclass(frozen=True)
class Wave:
    number: int
    tickets: tuple[PlannedTicket, ...]


@dataclass(frozen=True)
class RunPlan:
    run: RunMetadata
    waves: tuple[Wave, ...]

    @property
    def tickets(self):
        return tuple(ticket for wave in self.waves for ticket in wave.tickets)

    def wave(self, number):
        wanted = _wave_number(number)
        for wave in self.waves:
            if wave.number == wanted:
                return wave
        raise RunPlanError([f"run: the plan holds no wave {wanted}"])

    def ticket(self, identifier):
        wanted = _ticket_id(identifier)
        for ticket in self.tickets:
            if ticket.id == wanted:
                return ticket
        raise RunPlanError([f"run: the plan holds no ticket {wanted}"])

    def following_wave(self, number):
        current = self.wave(number)
        for index, wave in enumerate(self.waves):
            if wave == current:
                return self.waves[index + 1] if index + 1 < len(self.waves) else None
        return None

    def descendants(self, roots):
        wanted = {self.ticket(root).id for root in roots}
        found = []
        while True:
            frontier = [
                ticket.id for ticket in self.tickets
                if ticket.id not in wanted and ticket.id not in found
                and any(blocker in wanted or blocker in found for blocker in ticket.blocked_by)
            ]
            if not frontier:
                return tuple(found)
            found.extend(frontier)

    def append(self, ticket):
        """This plan with one more trailing Wave carrying that one ticket (ADR-0028).

        The appended ticket is blocked by the last ticket, in table order, of the Wave that was
        final before it: queued Waves run serially because they often share a root cause, so each
        one starts from the code the Wave before it merged. `Blocked by` is this operation's to
        set — a caller that names its own is refused rather than half-honoured — and the
        result is validated exactly as an approved table is, so a routing the table refuses is
        refused here in the same words.
        """
        if not isinstance(ticket, PlannedTicket):
            raise RunPlanError([f"run: {ticket!r} is not a planned ticket"])
        if ticket.blocked_by:
            raise RunPlanError([
                f"{ticket.id} {ticket.path}: names its own Blocked by"
                f" {', '.join('#' + blocker for blocker in ticket.blocked_by)} — appending owns"
                " that edge, so it is left unset"
            ])
        if not self.waves or not self.waves[-1].tickets:
            raise RunPlanError(["run: the plan carries no wave to append behind"])
        preceding = self.waves[-1].tickets[-1]
        appended = Wave(
            len(self.waves) + 1, (replace(ticket, blocked_by=(preceding.id,)),)
        )
        return _validate(RunPlan(self.run, self.waves + (appended,)))

    def write(self, path):
        """Write the existing Wave-table JSON representation."""
        path = pathlib.Path(path)
        try:
            path.write_text(
                json.dumps(_plan_object(_validate(self)), indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            raise RunPlanError([f"run: {path} could not be written: {error}"]) from error


def _wave_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RunPlanError([f"run: wave number {value!r} is not a positive integer"])
    if isinstance(value, str) and not value.strip().isdigit():
        raise RunPlanError([f"run: wave number {value!r} is not a positive integer"])
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise RunPlanError([f"run: wave number {value!r} is not a positive integer"]) from error
    if number < 1:
        raise RunPlanError([f"run: wave number {value!r} is not a positive integer"])
    return number


def _ticket_id(value):
    identifier = str(value).strip()
    if not identifier or not identifier.isdigit():
        raise RunPlanError([f"run: ticket identifier {value!r} is not numeric"])
    return identifier


def _sections(text):
    found = {}
    heading = None
    for line in text.splitlines():
        match = SECTION.match(line)
        if match:
            heading = match.group(1).lower()
            found[heading] = []
        elif heading is not None:
            found[heading].append(line)
    return {name: "\n".join(lines) for name, lines in found.items()}


def ticket_title(text, identifier):
    """The ticket's first Markdown heading, or its identifier when it has none."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return identifier


def ticket_section(text, wanted):
    """One Markdown section, including its heading, exactly as the source carries it."""
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        heading = SECTION.match(line.rstrip("\n"))
        if heading and start is not None:
            return "".join(lines[start:index]).rstrip("\n")
        if heading and heading.group(1).lower() == wanted:
            start = index
    return "" if start is None else "".join(lines[start:]).rstrip("\n")


def ticket_pointer(url):
    """The tracker-authority line shared by ticket and parent stubs."""
    return f"Ticket: {url} — the issue body and every comment are this ticket; read all of it."


def staged_text(kind, title, body, url=None):
    """That ticket as a Run directory holds it, pointing at its tracker authority.

    The Run directory's own form of a ticket, which is the Run plan's to say (ADR-0018): a github
    ticket is a stub carrying the pointer and the two machine sections the plan reads, and a local
    ticket is the file itself. Staging writes one at `/route` time and the Driver writes one for a
    ticket the Run queues into itself (ADR-0028); both render it from here, so a run directory
    holds one shape however the ticket got there.
    """
    if kind == GITHUB_TRACKER:
        machine_sections = [
            held for name in (ROUTING_SECTION, BLOCKED_BY_SECTION)
            if (held := ticket_section(body, name))
        ]
        suffix = "".join(f"\n\n{held}" for held in machine_sections)
        return f"# {title}\n\n{ticket_pointer(url)}{suffix}\n"
    return body


def ticket_dependencies(text):
    """The ordered ticket identifiers in one ticket's `Blocked by` section."""
    return tuple(BLOCKER.findall(_sections(text).get(BLOCKED_BY_SECTION, "")))


def _routing(section):
    values = {}
    for line in section.splitlines():
        match = ROUTING_LINE.match(line)
        if not match:
            continue
        key = match.group(1).strip().lower()
        value = match.group(2).strip()
        if key in ("workflow", "executor", "model", "effort", "account"):
            values[key] = value
        elif key == "review":
            words = value.split()
            values[key] = (
                {"vendor": words[0], "model": words[1], "effort": words[2]}
                if len(words) == 3 else value
            )
    return values


def _routing_vocabulary():
    try:
        with TEMPLATES.open("rb") as handle:
            templates = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        problem = f"run: routing templates {TEMPLATES} are unreadable: {error}"
        raise RunPlanError([problem]) from error
    return templates["workflows"], {
        str(alias).lower() for alias in templates["models"]["aliases"]
    }


def routing_faults(workflow, executor, model, effort, vocabulary=None):
    """Every fault in one ticket's four routing fields, in the Wave table's own words.

    The single place those four are judged, so a routing resolved anywhere else — the `[queued]`
    cell a coordinator queues a diagnosis on (ADR-0028) — is held to exactly what an approved
    table passes, and is refused in the same words. `vocabulary` is the `_routing_vocabulary`
    pair, passed in by a caller validating a whole table so the templates are read once.
    """
    workflows, aliases = vocabulary if vocabulary is not None else _routing_vocabulary()
    faults = []
    if workflow not in workflows:
        faults.append(f"Workflow `{workflow}` is outside {', '.join(sorted(workflows))}")
    if executor not in EXECUTORS:
        faults.append(f"Executor `{executor}` is outside {', '.join(EXECUTORS)}")
    if effort not in EFFORTS:
        faults.append(f"Effort `{effort}` is outside {', '.join(EFFORTS)}")
    fault = _alias_problem("Model", model, aliases)
    if fault:
        faults.append(fault)
    return faults


def review_faults(workflow, review, vocabulary=None):
    """Every fault in one ticket's review lane against its workflow, in the same words."""
    workflows, aliases = vocabulary if vocabulary is not None else _routing_vocabulary()
    wants_lane = bool(workflow in workflows and workflows[workflow]["review_lane"])
    if wants_lane and review is None:
        return ["lacks Review, which its workflow requires"]
    if review is None:
        return []
    if not wants_lane:
        return [f"carries a Review, which workflow `{workflow}` takes none of"]
    if not isinstance(review, ReviewLane):
        return ["Review is not a review lane"]
    faults = []
    if review.vendor not in REVIEW_VENDORS:
        faults.append(f"Review vendor `{review.vendor}` is outside {', '.join(REVIEW_VENDORS)}")
    if not review.model:
        faults.append("Review lacks model")
    else:
        fault = _alias_problem("Review model", review.model, aliases)
        if fault:
            faults.append(fault)
    if review.effort not in EFFORTS:
        faults.append(f"Review effort `{review.effort}` is outside {', '.join(EFFORTS)}")
    return faults


def model_problem(label, value):
    """Why one configured model is not a full model identifier, or None when it is."""
    _, aliases = _routing_vocabulary()
    return _alias_problem(label, value, aliases)


def _alias_problem(label, value, aliases):
    if not isinstance(value, str):
        return f"{label} `{value}` is not a model name"
    bare = CONTEXT_SUFFIX.sub("", value.strip())
    if bare.lower() in aliases or BARE_WORD.match(bare):
        return f"{label} `{value}` is an alias, not a full model ID"
    return None


def _find_cycle(tickets):
    edges = {ticket.id: ticket.blocked_by for ticket in tickets}
    walking, done = 1, 2
    marks = {}
    stack = []

    def walk(identifier):
        marks[identifier] = walking
        stack.append(identifier)
        for blocker in edges[identifier]:
            if blocker not in edges:
                continue
            state = marks.get(blocker)
            if state == walking:
                return stack[stack.index(blocker):] + [blocker]
            if state is None:
                found = walk(blocker)
                if found:
                    return found
        stack.pop()
        marks[identifier] = done
        return None

    for identifier in edges:
        if identifier not in marks:
            found = walk(identifier)
            if found:
                return found
    return None


def _validation_problems(plan, check_wave_layout=True):
    problems = configuration_problems(
        plan.run.repo_root,
        plan.run.repair_model,
        plan.run.witness_model,
        plan.run.witness_budget_usd,
        plan.run.tracker,
    )
    vocabulary = _routing_vocabulary()
    numbers = [wave.number for wave in plan.waves]
    expected_numbers = list(range(1, len(plan.waves) + 1))
    if numbers != expected_numbers:
        problems.append(
            "run: wave numbers are " + ", ".join(map(str, numbers))
            + ", not the ordered sequence " + ", ".join(map(str, expected_numbers))
        )
    for wave in plan.waves:
        if not wave.tickets:
            problems.append(f"run: wave {wave.number} carries no tickets")

    seen = set()
    for ticket in plan.tickets:
        faults = []
        required = {
            "id": "Ticket id",
            "title": "Title",
            "path": "Path",
            "workflow": "Workflow",
            "executor": "Executor",
            "model": "Model",
            "effort": "Effort",
        }
        for key, label in required.items():
            value = getattr(ticket, key)
            if not isinstance(value, str) or not value:
                faults.append(f"lacks {label}")
        if isinstance(ticket.id, str) and not ticket.id.isdigit():
            faults.append(f"Ticket id `{ticket.id}` is not numeric")
        if ticket.id in seen:
            faults.append("is listed twice")
        seen.add(ticket.id)
        faults.extend(routing_faults(
            ticket.workflow, ticket.executor, ticket.model, ticket.effort, vocabulary
        ))
        binding = ticket.binding
        if not isinstance(binding, accounts.Binding):
            faults.append("lacks Account binding")
        else:
            if not binding.directory:
                faults.append("lacks Account")
            elif not pathlib.Path(binding.directory).is_absolute():
                faults.append(f"Account `{binding.directory}` is not an absolute path")
            if not binding.mode:
                faults.append("lacks Account mode")
            elif binding.mode not in ACCOUNT_MODES:
                faults.append(
                    f"Account mode `{binding.mode}` is outside {', '.join(ACCOUNT_MODES)}"
                )
        faults.extend(review_faults(ticket.workflow, ticket.review, vocabulary))
        if ticket.queued is not None:
            if not isinstance(ticket.queued, Queued):
                faults.append("Queued is not a queued fact")
            else:
                if not isinstance(ticket.queued.source, str):
                    faults.append("Queued source is not a string")
                elif not ticket.queued.source.strip():
                    faults.append("Queued lacks source, the finding's own ticket")
                if ticket.queued.open not in OPEN_WORDS:
                    faults.append(
                        f"Queued open `{ticket.queued.open}` is outside"
                        f" {', '.join(OPEN_WORDS)}"
                    )
        # The optional strings, held to what a load of the written table would accept, so nothing
        # this validates can be written and then refused on the way back in.
        for key, label in (("slug", "Slug"), ("base_commit", "Base commit")):
            value = getattr(ticket, key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                faults.append(f"{label} is not a non-empty string")
        if ticket.executor == "codex" and plan.run.codex is None:
            faults.append("needs the run's codex bridge and state_dir")
        if faults:
            problems.append(f"{ticket.id} {ticket.path}: " + "; ".join(faults))

    identifiers = {ticket.id for ticket in plan.tickets}
    for ticket in plan.tickets:
        for blocker in ticket.blocked_by:
            if blocker not in identifiers:
                problems.append(
                    f"{ticket.id} {ticket.path}: is blocked by #{blocker}, which no ticket"
                    " of this plan carries"
                )
    cycle = _find_cycle(plan.tickets)
    if cycle:
        problems.append(
            "dependency graph: " + " → ".join(cycle) + " is a cycle, which no wave can order"
        )

    if not problems and check_wave_layout:
        expected = _assign_waves(plan.tickets)
        actual_ids = tuple(tuple(ticket.id for ticket in wave.tickets) for wave in plan.waves)
        expected_ids = tuple(tuple(ticket.id for ticket in wave.tickets) for wave in expected)
        if actual_ids != expected_ids:
            problems.append(
                f"run: waves {actual_ids!r} do not follow the dependency frontier {expected_ids!r}"
            )
    return problems


def _validate(plan):
    problems = _validation_problems(plan)
    if problems:
        raise RunPlanError(problems)
    return plan


def _string(values, key, label=None, optional=False):
    label = label or key
    value = values.get(key)
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RunPlanError([f"run: {label} is not a non-empty string"])
    return value


def _absolute(values, key, label=None, optional=False):
    value = _string(values, key, label, optional)
    if value is not None and not pathlib.Path(value).is_absolute():
        raise RunPlanError([f"run: {label or key} `{value}` is not an absolute path"])
    return value


def witness_defaults():
    """The witness's independently shipped model and budget defaults."""
    try:
        with DEFAULT_CONFIG.open("rb") as handle:
            witness = tomllib.load(handle).get("witness")
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RunPlanError([
            f"run: shipped witness defaults {DEFAULT_CONFIG} are unreadable: {error}"
        ]) from error
    if not isinstance(witness, dict):
        raise RunPlanError([
            f"run: shipped defaults {DEFAULT_CONFIG} carry no [witness] table"
        ])
    return {
        "model": witness.get("model"),
        "budget_usd": witness.get("budget_usd"),
    }


def witness_routing(model, budget_usd):
    """Resolve the witness executor, model and budget without consulting `[repair]`."""
    if model is None or budget_usd is None:
        defaults = witness_defaults()
        if model is None:
            model = defaults["model"]
        if budget_usd is None:
            budget_usd = defaults["budget_usd"]
    return WITNESS_EXECUTOR, model, budget_usd


def queued_defaults():
    """The shipped `[queued]` cell: the routing every queued ticket is opened on."""
    try:
        with DEFAULT_CONFIG.open("rb") as handle:
            queued = tomllib.load(handle).get(QUEUED_SECTION)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RunPlanError([
            f"run: shipped queued defaults {DEFAULT_CONFIG} are unreadable: {error}"
        ]) from error
    if not isinstance(queued, dict):
        raise RunPlanError([
            f"run: shipped defaults {DEFAULT_CONFIG} carry no [{QUEUED_SECTION}] table"
        ])
    return queued


def _queued_problems(faults):
    """The validator's own fault text, said of the `[queued]` cell that resolved to it."""
    return [f"queued: [{QUEUED_SECTION}] {fault}" for fault in faults]


def _queued_field(cell, key, label):
    value = cell.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RunPlanError([f"queued: [{label}] {key} is not a non-empty string"])
    return value


def queued_routing(project_cell):
    """Resolve the `[queued]` cell, a project's own overriding the shipped one field by field.

    The operator's standing approval for a routing the coordinator applies in their stead
    (ADR-0028), read the way every other cell of the config file is read: a project file names
    only the fields it retargets and inherits the rest. The review lane is resolved for the
    workflows that take one and dropped for the workflows that take none, so the resolved value is
    a routing the wave table already accepts.
    """
    shipped = queued_defaults()
    if project_cell is None:
        project_cell = {}
    if not isinstance(project_cell, dict):
        raise RunPlanError([
            f"queued: [{QUEUED_SECTION}] is not a table of routing fields"
        ])
    if QUEUED_ACCOUNT in project_cell or QUEUED_ACCOUNT in shipped:
        raise RunPlanError([
            f"queued: [{QUEUED_SECTION}] names an `{QUEUED_ACCOUNT}` — which subscription pays is"
            " not a fact about the kind of work, so a queued ticket names none and runs on the"
            " coordinator's own account; name it on the queue command instead"
        ])
    known = set(QUEUED_FIELDS) | {QUEUED_REVIEW}
    unknown = sorted(set(project_cell) - known) + sorted(set(shipped) - known)
    if unknown:
        raise RunPlanError([
            f"queued: [{QUEUED_SECTION}] carries an unknown field `{field}` — it is one of"
            f" {', '.join(sorted(known))}"
            for field in unknown
        ])
    cell = {**shipped, **project_cell}
    resolved = {
        key: _queued_field(cell, key, QUEUED_SECTION) for key in QUEUED_FIELDS
    }
    vocabulary = _routing_vocabulary()
    workflows, _ = vocabulary
    # Held to exactly what an approved Wave table passes, in the table's own words, and held here
    # rather than at the append: the coordinator's `--effort`, `--executor` and `--model`
    # overrides reach this cell, and everything refusable must be refused before `queue` opens a
    # tracker ticket that no failure afterwards can take back.
    faults = routing_faults(
        resolved["workflow"], resolved["executor"], resolved["model"], resolved["effort"],
        vocabulary,
    )
    shape = workflows.get(resolved["workflow"])
    if shape is None:
        # Without a shape there is no saying whether a lane is owed, so this is as far as one
        # resolution goes; the workflow fault is already in hand.
        raise RunPlanError(_queued_problems(faults))
    review = None
    if shape["review_lane"]:
        lanes = []
        for source in (shipped, project_cell):
            held = source.get(QUEUED_REVIEW)
            if held is not None and not isinstance(held, dict):
                raise RunPlanError([
                    f"queued: [{QUEUED_SECTION}.{QUEUED_REVIEW}] is not a table of lane fields"
                ])
            lanes.append(held or {})
        lane = {**lanes[0], **lanes[1]}
        if not lane:
            raise RunPlanError([
                f"queued: [{QUEUED_SECTION}.{QUEUED_REVIEW}] is missing, and workflow"
                f" `{resolved['workflow']}` takes a review lane"
            ])
        label = f"{QUEUED_SECTION}.{QUEUED_REVIEW}"
        review = ReviewLane(
            _queued_field(lane, "executor", label),
            _queued_field(lane, "model", label),
            _queued_field(lane, "effort", label),
        )
    faults += review_faults(resolved["workflow"], review, vocabulary)
    if faults:
        raise RunPlanError(_queued_problems(faults))
    return QueuedRouting(review=review, **resolved)


def configuration_problems(
    repo_root, repair_model, witness_model, witness_budget_usd, tracker
):
    """Problems in the configured run decisions, in the Run plan's vocabulary."""
    config = pathlib.Path(repo_root) / PROJECT_CONFIG_NAME
    problems = []
    if not isinstance(repair_model, str) or not repair_model.strip():
        problems.append(
            f"repair model: {config} names no [repair] model — the merge ladder's repair rung"
            " has no model to run on, and it takes a full model ID, never an alias"
        )
    else:
        fault = model_problem("`[repair] model`", repair_model)
        if fault:
            problems.append(f"repair model: {fault}")
    try:
        _, witness_model, witness_budget_usd = witness_routing(
            witness_model, witness_budget_usd
        )
    except RunPlanError as error:
        problems.extend(error.problems)
    else:
        if not isinstance(witness_model, str) or not witness_model.strip():
            problems.append(
                f"witness model: {config} and the shipped defaults name no [witness] model —"
                " fact-checking an escalation takes a full model ID, never an alias"
            )
        else:
            fault = model_problem("`[witness] model`", witness_model)
            if fault:
                problems.append(f"witness model: {fault}")
        if (
            isinstance(witness_budget_usd, bool)
            or not isinstance(witness_budget_usd, (int, float))
            or witness_budget_usd <= 0
        ):
            problems.append(
                f"witness budget: {config} and the shipped defaults do not name a positive"
                " [witness] budget_usd"
            )
    if not isinstance(tracker, str) or not tracker.strip():
        problems.append(
            f"tracker: {config} names no [tracker] kind — a merged ticket has nowhere to be"
            f" closed; it is one of {', '.join(TRACKERS)}"
        )
    elif tracker not in TRACKERS:
        problems.append(
            f"tracker: `{tracker}` is not a tracker this run closes tickets in — it is one of"
            f" {', '.join(TRACKERS)}, the two `references/trackers.md` declares exercised"
        )
    return problems


def _metadata(values, persisted=False):
    if not isinstance(values, dict):
        raise RunPlanError(["run: metadata is not an object"])
    required = (
        "repo_root", "crew_worktree", "spec_path", "integration_branch",
        "integration_base_commit",
        "coordinator_name", "coordinator_pid", "crew_skill_dir", "tmux_session",
        "permission_mode", "coordinator_config_home",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RunPlanError(["run: lacks " + ", ".join(missing)])
    repo_root = _absolute(values, "repo_root")
    crew_worktree = _absolute(values, "crew_worktree")
    spec_path = _absolute(values, "spec_path")
    integration_branch = _string(values, "integration_branch")
    integration_base_commit = _string(values, "integration_base_commit")
    coordinator_name = _string(values, "coordinator_name")
    coordinator_pid = values["coordinator_pid"]
    if (
        not isinstance(coordinator_pid, int)
        or isinstance(coordinator_pid, bool)
        or coordinator_pid < 1
    ):
        raise RunPlanError(["run: coordinator_pid is not a positive integer"])
    crew_skill_dir = _absolute(values, "crew_skill_dir")
    tmux_session = _string(values, "tmux_session")
    permission_mode = _string(values, "permission_mode")
    coordinator_config_home = _absolute(values, "coordinator_config_home")

    coordinator_session = values.get("coordinator_session", "")
    if not isinstance(coordinator_session, str):
        raise RunPlanError(["run: coordinator_session is not a string"])
    # Optional and defaulted for the same reason the session ID above is: a table written before
    # the field existed must still load, and every start refreshes it off the launcher's arguments
    # anyway, so a run already under way heals itself on its first resume rather than migrating.
    coordinator_address = values.get("coordinator_address", "")
    if not isinstance(coordinator_address, str):
        raise RunPlanError(["run: coordinator_address is not a string"])
    optional = {
        key: _string(values, key, optional=True)
        for key in ("base_branch", "repair_model", "tracker")
    }
    _, witness_model, witness_budget_usd = witness_routing(
        values.get("witness_model"), values.get("witness_budget_usd")
    )
    if not isinstance(witness_model, str) or not witness_model.strip():
        raise RunPlanError(["run: witness_model is not a non-empty string"])
    feature_dir = _absolute(values, "feature_dir", optional=True)
    declared_accounts = values.get("declared_accounts", [])
    if not isinstance(declared_accounts, list):
        raise RunPlanError(["run: declared_accounts is not a list"])
    if any(not isinstance(name, str) or not name.strip() for name in declared_accounts):
        raise RunPlanError(["run: declared_accounts must contain only non-empty strings"])

    codex = values.get("codex")
    codex_value = None
    if codex is not None:
        if not isinstance(codex, dict):
            raise RunPlanError(["run: codex is not an object"])
        missing_codex = [key for key in ("bridge", "state_dir") if not codex.get(key)]
        if missing_codex:
            raise RunPlanError(["run: codex lacks " + ", ".join(missing_codex)])
        codex_value = CodexConfig(
            _absolute(codex, "bridge", "codex.bridge"),
            _absolute(codex, "state_dir", "codex.state_dir"),
        )
    hook = values.get("launch_hook")
    hook_value = None
    if hook is not None:
        if not isinstance(hook, dict):
            raise RunPlanError(["run: launch_hook is not an object"])
        environment = hook.get("env", {})
        if not isinstance(environment, dict):
            raise RunPlanError(["run: launch_hook.env is not an object"])
        if any(
            not isinstance(name, str) or not name or not isinstance(value, str)
            for name, value in environment.items()
        ):
            raise RunPlanError(["run: launch_hook.env must map non-empty strings to strings"])
        # Project config spells no command as an empty string. The normalized Wave-table omits the
        # key when command is None, so only metadata loaded from that persistence form may lack it.
        if "command" not in hook:
            if not persisted:
                raise RunPlanError(["run: launch_hook.command is not a string"])
            command = None
        else:
            command = hook["command"]
            if not isinstance(command, str):
                raise RunPlanError(["run: launch_hook.command is not a string"])
            if command == "":
                command = None
        if command is not None or environment:
            hook_value = LaunchHook(command, tuple(environment.items()))
    return RunMetadata(
        repo_root=repo_root,
        crew_worktree=crew_worktree,
        spec_path=spec_path,
        integration_branch=integration_branch,
        integration_base_commit=integration_base_commit,
        coordinator_name=coordinator_name,
        coordinator_pid=coordinator_pid,
        crew_skill_dir=crew_skill_dir,
        tmux_session=tmux_session,
        permission_mode=permission_mode,
        coordinator_config_home=coordinator_config_home,
        coordinator_session=coordinator_session,
        coordinator_address=coordinator_address,
        base_branch=optional["base_branch"],
        feature_dir=feature_dir,
        repair_model=optional["repair_model"],
        witness_model=witness_model,
        witness_budget_usd=witness_budget_usd,
        tracker=optional["tracker"],
        declared_accounts=tuple(declared_accounts),
        codex=codex_value,
        launch_hook=hook_value,
    )


def _metadata_object(run):
    document = {
        "repo_root": run.repo_root,
        "crew_worktree": run.crew_worktree,
        "spec_path": run.spec_path,
        "integration_branch": run.integration_branch,
        "integration_base_commit": run.integration_base_commit,
        "coordinator_name": run.coordinator_name,
        "coordinator_pid": run.coordinator_pid,
        "coordinator_session": run.coordinator_session,
        "coordinator_address": run.coordinator_address,
        "crew_skill_dir": run.crew_skill_dir,
        "tmux_session": run.tmux_session,
        "permission_mode": run.permission_mode,
        "coordinator_config_home": run.coordinator_config_home,
        "declared_accounts": list(run.declared_accounts),
    }
    for key in (
        "base_branch", "feature_dir", "repair_model", "witness_model",
        "witness_budget_usd", "tracker",
    ):
        value = getattr(run, key)
        if value is not None:
            document[key] = value
    if run.codex is not None:
        document["codex"] = {"bridge": run.codex.bridge, "state_dir": run.codex.state_dir}
    if run.launch_hook is not None:
        document["launch_hook"] = {"env": dict(run.launch_hook.env)}
        if run.launch_hook.command is not None:
            document["launch_hook"]["command"] = run.launch_hook.command
    return document


def _ticket_object(ticket):
    document = {
        "id": ticket.id,
        "title": ticket.title,
        "path": ticket.path,
        "workflow": ticket.workflow,
        "executor": ticket.executor,
        "model": ticket.model,
        "effort": ticket.effort,
        "account": ticket.binding.directory,
        "account_mode": ticket.binding.mode,
        "blocked_by": list(ticket.blocked_by),
    }
    if ticket.review is not None:
        document["review"] = {
            "vendor": ticket.review.vendor,
            "model": ticket.review.model,
            "effort": ticket.review.effort,
        }
    if ticket.queued is not None:
        document["queued"] = {
            "source": ticket.queued.source,
            "open": ticket.queued.open,
        }
    if ticket.slug is not None:
        document["slug"] = ticket.slug
    if ticket.base_commit is not None:
        document["base_commit"] = ticket.base_commit
    return document


def _plan_object(plan):
    return {
        "run": _metadata_object(plan.run),
        "waves": [
            {
                "wave": wave.number,
                "tickets": [_ticket_object(ticket) for ticket in wave.tickets],
            }
            for wave in plan.waves
        ],
    }


def _ticket_string(values, key, label, optional=False):
    value = values.get(key)
    if optional and value is None:
        return None
    if value is None:
        return ""
    if not isinstance(value, str) or (optional and not value.strip()):
        identifier = values.get("id") or "(no id)"
        path = values.get("path") or ""
        raise RunPlanError([f"{identifier} {path}: {label} is not a non-empty string"])
    return value


def _loaded_ticket(value):
    if not isinstance(value, dict):
        raise RunPlanError([f"{value!r}: is not a ticket object"])
    identifier = _ticket_id(value.get("id")) if value.get("id") is not None else ""
    review = value.get("review")
    review_value = None
    if review is not None:
        if not isinstance(review, dict):
            raise RunPlanError([
                f"{identifier or '(no id)'} {value.get('path', '')}: Review is not a review lane"
            ])
        review_value = ReviewLane(
            _ticket_string(review, "vendor", "Review vendor"),
            _ticket_string(review, "model", "Review model"),
            _ticket_string(review, "effort", "Review effort"),
        )
    queued = value.get("queued")
    queued_value = None
    if queued is not None:
        where = f"{identifier or '(no id)'} {value.get('path', '')}"
        if not isinstance(queued, dict):
            raise RunPlanError([f"{where}: Queued is not a queued fact"])
        for key, label in (("source", "Queued source"), ("open", "Queued open")):
            if not isinstance(queued.get(key), str):
                raise RunPlanError([f"{where}: {label} is not a string"])
        queued_value = Queued(queued["source"], queued["open"])
    blocked_by = value.get("blocked_by", [])
    if not isinstance(blocked_by, list):
        raise RunPlanError([
            f"{identifier or '(no id)'} {value.get('path', '')}: blocked_by is not a list of"
            " ticket identifiers"
        ])
    return PlannedTicket(
        id=identifier,
        title=_ticket_string(value, "title", "Title"),
        path=_ticket_string(value, "path", "Path"),
        workflow=_ticket_string(value, "workflow", "Workflow"),
        executor=_ticket_string(value, "executor", "Executor"),
        model=_ticket_string(value, "model", "Model"),
        effort=_ticket_string(value, "effort", "Effort"),
        binding=accounts.Binding(
            _ticket_string(value, "account", "Account"),
            _ticket_string(value, "account_mode", "Account mode"),
        ),
        blocked_by=tuple(_ticket_id(blocker) for blocker in blocked_by),
        review=review_value,
        slug=_ticket_string(value, "slug", "Slug", optional=True),
        base_commit=_ticket_string(value, "base_commit", "Base commit", optional=True),
        queued=queued_value,
    )


def _loaded_plan(document):
    if not isinstance(document, dict):
        raise RunPlanError(["run: the plan is not a JSON object"])
    if not isinstance(document.get("run"), dict):
        raise RunPlanError(["run: the plan carries no run section"])
    raw_waves = document.get("waves")
    if not isinstance(raw_waves, list):
        raise RunPlanError(["run: the plan carries no list of waves"])
    waves = []
    for raw_wave in raw_waves:
        if not isinstance(raw_wave, dict):
            raise RunPlanError([f"run: {raw_wave!r} is not a wave object"])
        raw_tickets = raw_wave.get("tickets")
        if not isinstance(raw_tickets, list):
            raise RunPlanError([
                f"run: wave {raw_wave.get('wave')!r} carries no list of tickets"
            ])
        waves.append(Wave(
            _wave_number(raw_wave.get("wave")),
            tuple(_loaded_ticket(ticket) for ticket in raw_tickets),
        ))
    if not waves:
        raise RunPlanError(["run: the plan carries no waves"])
    return _validate(RunPlan(_metadata(document["run"], persisted=True), tuple(waves)))


def crew_state_dir(path):
    """Return the absolute state directory for either accepted Run-directory form."""
    path = pathlib.Path(path).resolve()
    return path if path.name == CREW_STATE_DIR_NAME else path / CREW_STATE_DIR_NAME


def resolve_run_dir(path):
    """Return an existing Run's state directory for either accepted input form."""
    argument = pathlib.Path(path)
    state_dir = crew_state_dir(argument)
    state_table = state_dir / "wave-table.json"
    if state_table.is_file():
        return state_dir
    raise RunPlanError([
        f"run: {argument} holds no run: no wave table at {state_table}; "
        f"accepted forms are <feature-dir> or <feature-dir>/{CREW_STATE_DIR_NAME}"
    ])


def load(path):
    """Strictly load one Run plan from the existing Wave-table JSON file."""
    path = pathlib.Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunPlanError([f"run: {path} is not a readable wave table: {error}"]) from error
    return _loaded_plan(document)


def _read_tickets(feature_dir, run):
    parsed = []
    problems = []
    for path in sorted(pathlib.Path(feature_dir).glob("*.md")):
        match = TICKET_FILE.match(path.name)
        if not match:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise RunPlanError([f"run: ticket {path} is unreadable: {error}"]) from error
        parts = _sections(text)
        routing = _routing(parts.get(ROUTING_SECTION, ""))
        account_name = routing.get("account")
        review = routing.get("review")
        if review is not None and not isinstance(review, dict):
            problems.append(
                f"{match.group(1)} {path}: Review must be `<vendor> <model> <effort>`"
            )
            continue
        review_value = (
            ReviewLane(review["vendor"], review["model"], review["effort"])
            if review is not None else None
        )
        parsed.append((path, match.group(1), text, parts, routing, account_name, review_value))

    named = [entry for entry in parsed if entry[5]]
    registered = {}
    registry = None
    if named:
        try:
            registry = accounts.registry_path()
            registered = accounts.load_registry(registry)
        except accounts.AccountsError as error:
            raise RunPlanError([f"account: {error}"]) from error
    tickets = []
    for path, identifier, text, parts, routing, account_name, review_value in parsed:
        binding = accounts.inherited(run.coordinator_config_home)
        if account_name:
            if run.declared_accounts and account_name not in run.declared_accounts:
                problems.append(
                    f"{identifier} {path}: names the account `{account_name}`, which the run"
                    f" config {pathlib.Path(run.repo_root) / PROJECT_CONFIG_NAME} does not"
                    f" declare — it declares {', '.join(run.declared_accounts)}"
                )
                continue
            try:
                directory = accounts.profile_directory(account_name, registered)
            except accounts.UnknownAccount as error:
                problems.append(f"{identifier} {path}: {error}")
                continue
            if not pathlib.Path(directory).is_dir():
                problems.append(
                    f"{identifier} {path}: names the account `{account_name}`, whose profile"
                    f" directory {directory} is not there — the registry {registry} names it"
                )
                continue
            binding = accounts.explicit(directory)
        tickets.append(PlannedTicket(
            id=identifier,
            title=ticket_title(text, identifier),
            path=str(path),
            workflow=str(routing.get("workflow") or ""),
            executor=str(routing.get("executor") or ""),
            model=str(routing.get("model") or ""),
            effort=str(routing.get("effort") or ""),
            binding=binding,
            blocked_by=ticket_dependencies(text),
            review=review_value,
        ))
    if problems:
        raise RunPlanError(problems)
    return tickets


def _assign_waves(tickets):
    waves = []
    remaining = list(tickets)
    placed = set()
    while remaining:
        frontier = tuple(
            ticket for ticket in remaining
            if all(blocker in placed for blocker in ticket.blocked_by)
        )
        if not frontier:
            raise RunPlanError([
                "dependency graph: the plan cannot be ordered because "
                + ", ".join(ticket.id for ticket in remaining)
                + " block each other"
            ])
        waves.append(Wave(len(waves) + 1, frontier))
        placed.update(ticket.id for ticket in frontier)
        remaining = [ticket for ticket in remaining if ticket.id not in placed]
    return tuple(waves)


def build(feature_dir, run_metadata):
    """Build one validated plan from ticket Markdown and Driver-supplied run metadata."""
    run = _metadata(run_metadata)
    tickets = _read_tickets(feature_dir, run)
    if not tickets:
        raise RunPlanError([
            f"run: {feature_dir} carries no tickets to route — a ticket is a `<number>.md` file"
            " at that directory's root (`01.md`, `07-slug.md`); a `tickets/` subdirectory is the"
            " archive layout of a finished run, not its input"
        ])
    candidate = RunPlan(run, (Wave(1, tuple(tickets)),))
    problems = _validation_problems(candidate, check_wave_layout=False)
    if problems:
        raise RunPlanError(problems)
    return _validate(RunPlan(run, _assign_waves(tickets)))
