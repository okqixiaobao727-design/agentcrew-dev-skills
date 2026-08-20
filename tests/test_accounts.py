#!/usr/bin/env python3
"""The account binding contract: what a resolved account is, and what it does to an environment.

Every Claude process of a ticket — the implementer child's window, the reviewer, the merge-repair
session, the wake monitor — asks this module the same question, so the answers are pinned here
rather than only through the four consumers that ask them. The distinction under test is the one
#110 turned on: a configuration home spelled into `CLAUDE_CONFIG_DIR` and a `CLAUDE_CONFIG_DIR`
left alone reach the same directory and are not the same login.
"""

import os
import pathlib
import sys
import unittest


ASSETS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "crew" / "assets"
sys.path.insert(0, str(ASSETS))

import accounts  # noqa: E402


PROFILE = "/profiles/second"
COORDINATOR_HOME = "/home/operator/.claude"


class BindingTests(unittest.TestCase):
    """What the wave table's two account keys mean, read back off a row."""

    def test_a_row_that_named_an_account_is_bound_to_it_explicitly(self):
        binding = accounts.row_binding(
            {"account": PROFILE, "account_mode": accounts.EXPLICIT}
        )

        self.assertEqual(binding, accounts.explicit(PROFILE))

    def test_a_row_that_named_none_is_bound_to_the_coordinators_home_inherited(self):
        binding = accounts.row_binding(
            {"account": COORDINATOR_HOME, "account_mode": accounts.INHERITED}
        )

        self.assertEqual(binding, accounts.inherited(COORDINATOR_HOME))

    def test_a_row_carrying_no_account_at_all_carries_no_binding(self):
        """The candidate table the driver's preflight renders, before any account is resolved."""
        self.assertIsNone(accounts.row_binding({"id": "01"}))
        self.assertIsNone(accounts.row_binding({"account": ""}))

    def test_a_row_from_before_the_mode_existed_is_read_as_the_selection_it_made(self):
        """A table the first account release wrote set the variable for every row it wrote."""
        self.assertEqual(accounts.row_binding({"account": PROFILE}), accounts.explicit(PROFILE))

    def test_a_mode_the_table_cannot_mean_is_not_read_as_inheriting(self):
        """The renderer refuses such a row; nothing downstream may quietly widen it either."""
        self.assertEqual(
            accounts.row_binding({"account": PROFILE, "account_mode": "maybe"}),
            accounts.explicit(PROFILE),
        )

    def test_two_bindings_on_one_directory_in_two_modes_are_two_bindings(self):
        """What keeps a wave's monitors and live sources from collapsing into one another."""
        self.assertNotEqual(accounts.explicit(PROFILE), accounts.inherited(PROFILE))
        self.assertEqual(
            len({accounts.explicit(PROFILE), accounts.inherited(PROFILE)}), 2
        )


class EnvironmentTests(unittest.TestCase):
    """The one place a binding becomes an environment, which no consumer re-derives."""

    def test_an_explicit_binding_sets_the_one_variable_a_login_is_scoped_to(self):
        self.assertEqual(
            accounts.environment_delta(accounts.explicit(PROFILE)),
            {"CLAUDE_CONFIG_DIR": PROFILE},
        )

    def test_an_inherited_binding_adds_nothing_at_all(self):
        """Not the default home spelled out: that is a login that can fail where this one works."""
        self.assertEqual(accounts.environment_delta(accounts.inherited(COORDINATOR_HOME)), {})

    def test_a_missing_binding_adds_nothing_either(self):
        self.assertEqual(accounts.environment_delta(None), {})

    def test_an_explicit_binding_moves_the_callers_environment_and_nothing_else(self):
        base = {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": COORDINATOR_HOME}

        environment = accounts.process_environment(accounts.explicit(PROFILE), base)

        self.assertEqual(environment, {"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": PROFILE})

    def test_an_inherited_binding_hands_back_no_environment_to_start_a_process_in(self):
        """None, which `subprocess` reads as "leave it exactly as it is" — the whole promise."""
        self.assertIsNone(
            accounts.process_environment(accounts.inherited(COORDINATOR_HOME), {"PATH": "/usr/bin"})
        )
        self.assertIsNone(accounts.process_environment(None))

    def test_the_variable_is_the_one_claude_code_scopes_a_login_to(self):
        self.assertEqual(accounts.CONFIG_HOME_VARIABLE, "CLAUDE_CONFIG_DIR")


class LoginHomeTests(unittest.TestCase):
    """Which account answered a read, for the reads a login answers rather than a directory."""

    def test_an_explicit_bindings_login_is_the_account_it_names(self):
        self.assertEqual(accounts.login_home(accounts.explicit(PROFILE)), PROFILE)

    def test_an_inherited_bindings_login_is_the_readers_own(self):
        """None, which every reader here resolves the same way it resolves an unset home.

        It is the path spelling of the same rule `environment_delta` answers with, so the file a
        machine-wide answer is filed under is the file of the login that gave it.
        """
        self.assertIsNone(accounts.login_home(accounts.inherited(COORDINATOR_HOME)))
        self.assertIsNone(accounts.login_home(None))

    def test_the_recorded_directory_is_where_that_tickets_files_are(self):
        """Carried in both modes: a child writes its session under the home it was launched with,
        and the log records that home for attribution."""
        binding = accounts.inherited(COORDINATOR_HOME)

        self.assertEqual(binding.directory, COORDINATOR_HOME)
        self.assertEqual(binding.mode, accounts.INHERITED)


class RegistryLocationTests(unittest.TestCase):
    """The one rule the binding half of this module depends on: the registry is not per profile."""

    def test_a_relative_override_is_refused_rather_than_resolved(self):
        os.environ[accounts.REGISTRY_OVERRIDE] = "accounts.toml"
        self.addCleanup(os.environ.pop, accounts.REGISTRY_OVERRIDE, None)

        with self.assertRaises(accounts.AccountsError) as refused:
            accounts.registry_path()

        self.assertIn(accounts.REGISTRY_OVERRIDE, str(refused.exception))


if __name__ == "__main__":
    unittest.main()
