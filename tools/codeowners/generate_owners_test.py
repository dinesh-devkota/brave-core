#!/usr/bin/env python3
# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for generate_owners.py.

Owners are checked by reading the generated text the way code-owners reads
`OWNERS` files, independently of the generator's own lookup.
"""

import fnmatch
import posixpath
import unittest

import generate_owners
from generate_owners import GitHubOwners, OwnersError, generate

GITHUB = GitHubOwners(
    users={
        'alice': 'alice@brave.com',
        'bob': 'bob@brave.com',
        'carol': 'carol@brave.com',
        'bot': None,
    },
    teams={
        'rust': ['alice'],
        'wallet': ['bob', 'bot'],
        'deps': ['carol'],
    },
)


def _glob_matches(glob: str, path: str) -> bool:
    """`{**/,}<glob>` in Java glob syntax, for the globs the tests use."""
    regex = fnmatch.translate(glob).replace('.*', '[^/]*')
    return any(
        fnmatch.re.fullmatch(regex, '/'.join(path.split('/')[depth:]))
        for depth in range(len(path.split('/')))
    )


def owners_of(path: str, outputs: dict[str, str]) -> set[str]:
    """*path*'s owners in *outputs*, as code-owners reads `OWNERS` files."""

    def grants(line: str) -> set[str]:
        if line.startswith('file://'):
            text = outputs[line.removeprefix('file://')]
            entries = (
                entry.split('#', 1)[0].strip() for entry in text.splitlines()
            )
            return {
                entry
                for entry in entries
                if entry
                and not entry.startswith('per-file ')
                and entry != 'set noparent'
            }
        return {line}

    found: set[str] = set()
    directory = path
    while directory:
        directory = posixpath.dirname(directory)
        text = outputs.get(posixpath.join(directory, 'OWNERS'))
        if text is None:
            continue
        relative = posixpath.relpath(path, directory) if directory else path
        noparent = False
        everyone: set[str] = set()
        per_file: set[str] = set()
        per_file_noparent = False
        for line in text.splitlines():
            if not line or line.startswith('#'):
                continue
            if line.startswith('per-file '):
                glob, _, grant = line.removeprefix('per-file ').partition('=')
                if not _glob_matches(glob, relative):
                    continue
                if grant == 'set noparent':
                    per_file_noparent = True
                else:
                    per_file |= grants(grant)
            elif line == 'set noparent':
                noparent = True
            else:
                everyone |= grants(line)
        if per_file_noparent:
            return found | per_file
        found |= everyone | per_file
        if noparent:
            return found
    return found or {'*'}


class GenerateTest(unittest.TestCase):
    def check(self, codeowners: str, expected: dict[str, set[str]]) -> dict:
        outputs = generate(codeowners, expected, GITHUB)
        for path, owners in expected.items():
            with self.subTest(path=path):
                self.assertEqual(owners_of(path, outputs), owners)
        return outputs

    def test_later_rule_for_whole_tree_wins_over_directory(self):
        self.check(
            'a/ @brave/wallet\n*.rs @brave/rust\n',
            {
                'a/b.cc': {'bob@brave.com'},
                'a/c/d.rs': {'alice@brave.com'},
                'e.rs': {'alice@brave.com'},
                'f.cc': {'*'},
            },
        )

    def test_later_directory_rule_wins_over_whole_tree(self):
        self.check(
            '*.rs @brave/rust\na/ @brave/wallet\n',
            {
                'a/b.rs': {'bob@brave.com'},
                'c.rs': {'alice@brave.com'},
            },
        )

    def test_anchored_file_does_not_spread_to_subdirectories(self):
        outputs = self.check(
            '/DEPS @brave/deps\n',
            {
                'DEPS': {'carol@brave.com'},
                'a/DEPS': {'*'},
                'a/b/DEPS': {'*'},
                'c/x.cc': {'*'},
            },
        )
        # One override covers all of `a`.
        self.assertNotIn('a/b/OWNERS', outputs)

    def test_glob_below_a_directory(self):
        self.check(
            'chromium_src/ @brave/deps\nchromium_src/**/*.ts @brave/rust\n',
            {
                'chromium_src/a/b.ts': {'alice@brave.com'},
                'chromium_src/a/b.cc': {'carol@brave.com'},
                'other/c.ts': {'*'},
            },
        )

    def test_directories_named_anywhere(self):
        self.check(
            '**/renderer/**/* @brave/wallet\n',
            {
                'renderer/a.cc': {'bob@brave.com'},
                'x/renderer/y/b.cc': {'bob@brave.com'},
                'x/browser/c.cc': {'*'},
            },
        )

    def test_rule_without_owners_needs_no_review(self):
        self.check(
            '*.grd @brave/rust\na/b.grd\n',
            {
                'a/b.grd': {'*'},
                'a/c.grd': {'alice@brave.com'},
            },
        )

    def test_several_owners_and_users(self):
        self.check(
            'a/x.h @brave/wallet @carol\n',
            {
                'a/x.h': {'bob@brave.com', 'carol@brave.com'},
                'a/y.h': {'*'},
            },
        )

    def test_teams_are_imported_from_their_lists(self):
        outputs = self.check(
            'a/ @brave/wallet\nc/x.h @brave/wallet\n*.rs @brave/rust\n',
            {
                'a/1.cc': {'bob@brave.com'},
                'c/x.h': {'bob@brave.com'},
                'd/e.rs': {'alice@brave.com'},
            },
        )
        self.assertEqual(
            outputs['a/OWNERS'],
            'set noparent\nfile://tools/codeowners/WALLET_OWNERS\n',
        )
        self.assertIn(
            'per-file x.h=file://tools/codeowners/WALLET_OWNERS',
            outputs['c/OWNERS'],
        )
        self.assertIn(
            'per-file *.rs=file://tools/codeowners/RUST_OWNERS',
            outputs['OWNERS'],
        )
        self.assertEqual(
            outputs['tools/codeowners/RUST_OWNERS'],
            '# The members of @brave/rust on GitHub.\n'
            'alice@brave.com  # @alice\n',
        )

    def test_team_lists_skip_accounts_without_email(self):
        outputs = generate('a/ @brave/wallet\n', ['a/b.cc'], GITHUB)
        self.assertEqual(
            outputs['tools/codeowners/WALLET_OWNERS'],
            '# The members of @brave/wallet on GitHub.\n'
            'bob@brave.com  # @bob\n'
            '# @bot has no Gerrit account.\n',
        )

    def test_comments_are_carried_over(self):
        outputs = self.check(
            '# Rust\n'
            '# code\n'
            '*.rs @brave/rust\n'
            '# Just the next rule.\n'
            'Cargo.toml @carol\n'
            '*.toml @carol\n'
            '\n'
            '# Wallet\n'
            'a/ @bob\n',
            {
                'x.rs': {'alice@brave.com'},
                'Cargo.toml': {'carol@brave.com'},
                'y.toml': {'carol@brave.com'},
                'a/b.cc': {'bob@brave.com'},
            },
        )
        self.assertEqual(
            outputs['OWNERS'],
            '# Rust\n'
            '# code\n'
            'per-file *.rs=set noparent\n'
            'per-file *.rs=file://tools/codeowners/RUST_OWNERS\n'
            '\n'
            '# Just the next rule.\n'
            'per-file Cargo.toml=set noparent\n'
            'per-file Cargo.toml=carol@brave.com\n'
            '\n'
            '# Rust\n'
            '# code\n'
            'per-file *.toml=set noparent\n'
            'per-file *.toml=carol@brave.com\n',
        )
        self.assertEqual(
            outputs['a/OWNERS'], '# Wallet\nset noparent\nbob@brave.com\n'
        )

    def test_rules_matching_nothing_are_dropped(self):
        outputs = generate('missing/dir/x.cc @carol\n', ['a.cc'], GITHUB)
        self.assertEqual(list(outputs), [])

    def test_paths_differing_only_in_case_fail(self):
        with self.assertRaisesRegex(OwnersError, 'a/OWNERS and a/owners'):
            generate('a/ @carol\n', ['a/b.cc', 'a/owners/c.py'], GITHUB)

    def test_unknown_login_fails(self):
        with self.assertRaisesRegex(OwnersError, '@dave'):
            generate('a/ @dave\n', ['a/b.cc'], GITHUB)

    def test_unknown_team_fails(self):
        with self.assertRaisesRegex(OwnersError, '@brave/nobody'):
            generate('a/ @brave/nobody\n', ['a/b.cc'], GITHUB)


class TeamListsTest(unittest.TestCase):
    def test_parse_reads_members_and_accounts_without_email(self):
        github = GitHubOwners.parse(
            {
                'tools/codeowners/WALLET_OWNERS': (
                    '# The members of @brave/wallet on GitHub.\n'
                    'bob@brave.com  # @bob\n'
                    '# @bot has no Gerrit account.\n'
                ),
                'tools/codeowners/RUST_OWNERS': (
                    '# The members of @brave/rust on GitHub.\n'
                    'alice@brave.com  # @alice\n'
                    'bob@brave.com  # @bob\n'
                ),
            }
        )
        self.assertEqual(
            github.users,
            {'alice': 'alice@brave.com', 'bob': 'bob@brave.com', 'bot': None},
        )
        self.assertEqual(github.teams['wallet'], ['bob', 'bot'])
        self.assertEqual(
            github.team_emails('rust'), ['alice@brave.com', 'bob@brave.com']
        )

    def test_lists_round_trip(self):
        text = (
            '# The members of @brave/wallet on GitHub.\n'
            'bob@brave.com  # @bob\n'
            '# @bot has no Gerrit account.\n'
        )
        github = GitHubOwners.parse({'tools/codeowners/WALLET_OWNERS': text})
        self.assertEqual(github.render_list('wallet'), text)

    def test_parse_rejects_a_line_without_a_login(self):
        with self.assertRaisesRegex(OwnersError, 'WALLET_OWNERS:2'):
            GitHubOwners.parse(
                {
                    'tools/codeowners/WALLET_OWNERS': (
                        '# The members of @brave/wallet on GitHub.\n'
                        'bob@brave.com\n'
                    )
                }
            )

    def test_parse_rejects_a_list_under_another_name(self):
        with self.assertRaisesRegex(OwnersError, 'whose list is'):
            GitHubOwners.parse(
                {
                    'tools/codeowners/OTHER_OWNERS': (
                        '# The members of @brave/wallet on GitHub.\n'
                    )
                }
            )

    def test_parse_rejects_two_emails_for_one_login(self):
        with self.assertRaisesRegex(OwnersError, '@bob has two emails'):
            GitHubOwners.parse(
                {
                    'tools/codeowners/WALLET_OWNERS': (
                        '# The members of @brave/wallet on GitHub.\n'
                        'bob@brave.com  # @bob\n'
                    ),
                    'tools/codeowners/RUST_OWNERS': (
                        '# The members of @brave/rust on GitHub.\n'
                        'robert@brave.com  # @bob\n'
                    ),
                }
            )

    def test_update_teams_takes_members_from_github(self):
        github = generate_owners.update_teams(
            GITHUB, 'a/ @brave/wallet\n', lambda team: ['carol', 'bob']
        )
        self.assertEqual(github.teams['wallet'], ['bob', 'carol'])
        self.assertEqual(github.teams['rust'], ['alice'])

    def test_update_teams_needs_an_email_for_new_members(self):
        with self.assertRaisesRegex(OwnersError, 'WALLET_OWNERS: @dave'):
            generate_owners.update_teams(
                GITHUB, 'a/ @brave/wallet\n', lambda team: ['bob', 'dave']
            )


class IsGeneratedTest(unittest.TestCase):
    def test_owners_and_team_files(self):
        self.assertTrue(generate_owners.is_generated('OWNERS'))
        self.assertTrue(generate_owners.is_generated('a/b/OWNERS'))
        self.assertTrue(
            generate_owners.is_generated('tools/codeowners/SEC_TEAM_OWNERS')
        )
        self.assertFalse(generate_owners.is_generated('a/b_OWNERS'))
        self.assertFalse(generate_owners.is_generated('.github/CODEOWNERS'))


if __name__ == '__main__':
    unittest.main()
