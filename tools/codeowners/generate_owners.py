#!/usr/bin/env python3
# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Generates the OWNERS files that mirror `.github/CODEOWNERS`.

Gerrit's code-owners plugin reads `OWNERS` files in the `find-owners` syntax,
not `CODEOWNERS`, so this writes `OWNERS` files that give every tracked file
the owners `.github/CODEOWNERS` gives it, in the style of Chromium's. GitHub
teams cannot own files there, so each team's members are listed in
`tools/codeowners/<TEAM>_OWNERS`, which the `OWNERS` files import. Those lists
are also where GitHub logins are mapped to emails: each member's line is
`<email>  # @<login>`, and an account that is not a person is a
`# @<login> has no Gerrit account.` line.

The two formats resolve owners differently. In `CODEOWNERS` the last matching
rule wins. In `OWNERS` files the nearest file to a path decides, `set
noparent` stops the lookup there, and a `per-file` glob matches in its
directory and every subdirectory. So each rule is placed at its directory
with `set noparent`, later rules for a broader directory are repeated where
an earlier, deeper rule would hide them, and every tracked file is then
checked against both, overriding the owners of the files that still differ.

Usage:
    tools/codeowners/generate_owners.py            # writes the OWNERS files
    tools/codeowners/generate_owners.py --check    # fails if they are stale
    tools/codeowners/generate_owners.py --update-teams  # refreshes the members
"""

from __future__ import annotations

import argparse
import bisect
import functools
import posixpath
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

CODEOWNERS = '.github/CODEOWNERS'
TEAMS_DIR = 'tools/codeowners'
ORG = 'brave'

# Owned by all users: what a path with no `CODEOWNERS` owners resolves to.
ALL_USERS = '*'

_GLOB_CHARS = re.compile(r'[*?\[\]{}]')
_GLOB_ESCAPE = re.compile(r'[*?\[\]{}\\,]')

# Fixing a stale owner can make a subdirectory differ, so the comparison is
# repeated; real trees settle in a few rounds.
_MAX_ROUNDS = 20


class OwnersError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    """A `CODEOWNERS` line."""

    index: int
    pattern: str
    owners: tuple[str, ...]
    # The comment lines `CODEOWNERS` gives the rule.
    comment: tuple[str, ...] = ()

    @property
    def directory_only(self) -> bool:
        return self.pattern.endswith('/')

    @property
    def segments(self) -> list[str]:
        return self.pattern.strip('/').split('/')

    @property
    def anchored(self) -> bool:
        """Whether the pattern is relative to the root, as in `.gitignore`:
        it has a slash other than a trailing one."""
        return '/' in self.pattern.rstrip('/')

    def regex(self) -> re.Pattern[str]:
        body = _codeowners_regex('/'.join(self.segments))
        return re.compile(body if self.anchored else f'(?:.*/)?{body}')


def _codeowners_regex(glob: str) -> str:
    """A `CODEOWNERS` glob as a regex for a whole path."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif glob.startswith('**', i):
            out.append('.*')
            i += 2
        elif glob[i] == '*':
            out.append('[^/]*')
            i += 1
        elif glob[i] == '?':
            out.append('[^/]')
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return ''.join(out)


@functools.cache
def _find_owners_regex(glob: str) -> re.Pattern[str]:
    """A `per-file` glob as code-owners matches it: Java glob syntax, with
    `{**/,}` in front so that it matches in every subdirectory."""
    out = ['(?:.*/)?']
    i = 0
    depth = 0
    while i < len(glob):
        char = glob[i]
        if glob.startswith('**', i):
            out.append('.*')
            i += 2
            continue
        if char == '*':
            out.append('[^/]*')
        elif char == '?':
            out.append('[^/]')
        elif char == '{':
            out.append('(?:')
            depth += 1
        elif char == '}' and depth:
            out.append(')')
            depth -= 1
        elif char == ',' and depth:
            out.append('|')
        elif char == '\\' and i + 1 < len(glob):
            out.append(re.escape(glob[i + 1]))
            i += 1
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile(''.join(out))


def parse_codeowners(text: str) -> list[Rule]:
    """The rules, each with its comment: the comment opening its paragraph,
    or the one right above it when that comment follows another rule."""
    rules = []
    heading: tuple[str, ...] = ()
    pending: list[str] = []
    after_rule = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            heading, pending, after_rule = (), [], False
        elif line.startswith('#'):
            pending.append(line)
        else:
            if not after_rule:
                heading = tuple(pending)
            pattern, *owners = line.split()
            comment = tuple(pending) if pending else heading
            rules.append(Rule(len(rules), pattern, tuple(owners), comment))
            pending, after_rule = [], True
    return rules


@dataclass(frozen=True)
class Grant:
    """Owners in an `OWNERS` file: the lines naming them, the emails those
    lines come to, the `CODEOWNERS` rule they are for, and its comment."""

    lines: tuple[str, ...]
    emails: frozenset[str]
    rule: int
    comment: tuple[str, ...] = ()


@dataclass
class Directory:
    """The `OWNERS` file of one directory, in the subset of the
    `find-owners` syntax this writes: `set noparent` with owners for the
    whole directory, and `per-file` globs that each `set noparent`."""

    owners: Grant | None = None
    per_file: dict[str, Grant] = field(default_factory=dict)

    def set_owners(self, grant: Grant) -> None:
        if self.owners is None or self.owners.rule <= grant.rule:
            self.owners = grant

    def set_per_file(self, glob: str, grant: Grant) -> None:
        current = self.per_file.get(glob)
        if current is None or current.rule <= grant.rule:
            self.per_file[glob] = grant

    def render(self) -> str:
        """The file, laid out as in Chromium: the directory's owners first,
        then the `per-file` lines in `CODEOWNERS` order, a blank line and the
        rule's comment before each group."""
        groups = []
        if self.owners is not None:
            groups.append(
                [*self.owners.comment, 'set noparent', *self.owners.lines]
            )
        current: list[str] = []
        comment = self.owners.comment if self.owners else None
        for glob, grant in sorted(
            self.per_file.items(), key=lambda item: (item[1].rule, item[0])
        ):
            if grant.comment != comment or not current:
                if current:
                    groups.append(current)
                current = (
                    [] if grant.comment == comment else list(grant.comment)
                )
                comment = grant.comment
            current.append(f'per-file {glob}=set noparent')
            current.extend(f'per-file {glob}={line}' for line in grant.lines)
        if current:
            groups.append(current)
        return '\n\n'.join('\n'.join(group) for group in groups) + '\n'


_LIST_HEADER = re.compile(
    rf'# The members of @{ORG}/(?P<team>[A-Za-z0-9_.-]+) on GitHub\.'
)
_LIST_MEMBER = re.compile(
    r'(?P<email>[^\s#@]+@[^\s#]+)  # @(?P<login>[A-Za-z0-9-]+)'
)
_LIST_NO_ACCOUNT = re.compile(
    r'# @(?P<login>[A-Za-z0-9-]+) has no Gerrit account\.'
)


@dataclass(frozen=True)
class GitHubOwners:
    """Who `CODEOWNERS` names: emails by login, and members by team."""

    users: Mapping[str, str | None]
    teams: Mapping[str, list[str]]

    @staticmethod
    def parse(lists: Mapping[str, str]) -> GitHubOwners:
        """The team lists, given as {path: text}."""
        users: dict[str, str | None] = {}
        teams: dict[str, list[str]] = {}
        for path, text in sorted(lists.items()):
            lines = text.splitlines()
            header = _LIST_HEADER.fullmatch(lines[0]) if lines else None
            if not header:
                raise OwnersError(
                    f'{path} must start with "# The members of @{ORG}/<team> '
                    'on GitHub."'
                )
            team = header['team']
            if team_list(team) != path:
                raise OwnersError(
                    f'{path} lists @{ORG}/{team}, whose list is '
                    f'{team_list(team)}'
                )
            members = []
            for number, line in enumerate(lines[1:], start=2):
                if member := _LIST_MEMBER.fullmatch(line):
                    login, email = member['login'], member['email']
                elif no_account := _LIST_NO_ACCOUNT.fullmatch(line):
                    login, email = no_account['login'], None
                else:
                    raise OwnersError(
                        f'{path}:{number}: expected "<email>  # @<login>" or '
                        f'"# @<login> has no Gerrit account.", not {line!r}'
                    )
                if users.get(login, email) != email:
                    raise OwnersError(
                        f'@{login} has two emails in {TEAMS_DIR}: '
                        f'{users[login]} and {email}'
                    )
                users[login] = email
                members.append(login)
            teams[team] = members
        return GitHubOwners(users, teams)

    @staticmethod
    def load(root: Path) -> GitHubOwners:
        return GitHubOwners.parse(
            {
                f'{TEAMS_DIR}/{path.name}': path.read_text()
                for path in (root / TEAMS_DIR).glob('*_OWNERS')
            }
        )

    def email(self, login: str) -> str | None:
        """*login*'s email, or None for an account that is not a person."""
        if login not in self.users:
            raise OwnersError(
                f'@{login} is in none of the lists in {TEAMS_DIR}; add '
                f'"<email>  # @{login}" to the list of a team they are in'
            )
        return self.users[login]

    def team_emails(self, team: str) -> list[str]:
        if team not in self.teams:
            raise OwnersError(
                f'{team_list(team)} is missing; add it with the members of '
                f'@{ORG}/{team}, or run {TEAMS_DIR}/generate_owners.py '
                '--update-teams'
            )
        emails = (self.email(login) for login in self.teams[team])
        return sorted({email for email in emails if email}, key=str.lower)

    def render_list(self, team: str) -> str:
        """*team*'s list: its people by email, then its accounts that are
        not people."""
        logins = self.teams[team]
        people = sorted(
            (
                (email, login)
                for login in logins
                if (email := self.users[login])
            ),
            key=lambda member: (member[0].lower(), member[1].lower()),
        )
        others = sorted(
            (login for login in logins if not self.users[login]), key=str.lower
        )
        return ''.join(
            [
                f'# The members of @{ORG}/{team} on GitHub.\n',
                *(f'{email}  # @{login}\n' for email, login in people),
                *(f'# @{login} has no Gerrit account.\n' for login in others),
            ]
        )


def team_list(team: str) -> str:
    """Where a team's members are listed."""
    name = team.removesuffix('-owners').upper().replace('-', '_')
    return f'{TEAMS_DIR}/{name}_OWNERS'


class Resolver:
    """Writes `CODEOWNERS` owners as `OWNERS` lines: a team as an import of
    its list, a user as their email."""

    def __init__(self, rules: list[Rule], github: GitHubOwners):
        self.rules = rules
        self.github = github
        self.imports = {
            team_list(team): frozenset(github.team_emails(team))
            for team in {
                _team(owner)
                for rule in rules
                for owner in rule.owners
                if _team(owner)
            }
        }

    def grant(self, rule: int) -> Grant:
        """*rule*'s owners: all users when there are none, as a `CODEOWNERS`
        rule without owners asks for no review."""
        imports = []
        emails = set()
        for owner in self.rules[rule].owners:
            if team := _team(owner):
                imports.append(f'file://{team_list(team)}')
            elif email := self.github.email(owner.removeprefix('@')):
                emails.add(email)
        lines = tuple(sorted(set(imports))) + tuple(
            sorted(emails, key=str.lower)
        )
        lines = lines or (ALL_USERS,)
        return Grant(lines, self.emails(lines), rule, self.rules[rule].comment)

    def emails(self, lines: Iterable[str]) -> frozenset[str]:
        out: set[str] = set()
        for line in lines:
            if line.startswith('file://'):
                out |= self.imports[line.removeprefix('file://')]
            else:
                out.add(line)
        return frozenset(out)


def _team(owner: str) -> str | None:
    """The team *owner* names, if it names one."""
    prefix = f'@{ORG}/'
    return owner.removeprefix(prefix) if owner.startswith(prefix) else None


def _parent(path: str) -> str:
    return posixpath.dirname(path)


def _ancestors(path: str) -> list[str]:
    """The directories above *path*, nearest first, ending at the root."""
    out = []
    while path:
        path = _parent(path)
        out.append(path)
    return out


def _is_within(path: str, directory: str) -> bool:
    return (
        not directory or path == directory or path.startswith(directory + '/')
    )


def _escape(name: str) -> str:
    return _GLOB_ESCAPE.sub(lambda m: '\\' + m.group(0), name)


class Tree:
    """The tracked files, and lookups over them."""

    def __init__(self, files: Iterable[str]):
        self.files = sorted(files)
        self.ancestors = {f: _ancestors(f) for f in self.files}
        self.directories = sorted(
            {d for ancestors in self.ancestors.values() for d in ancestors}
        )

    def files_matching(self, rule: Rule) -> set[str]:
        """The files *rule* covers: those it matches, and those below the
        directories it matches."""
        regex = rule.regex()
        candidates = self.files
        if rule.anchored:
            literal = _GLOB_CHARS.split(rule.pattern.lstrip('/'), 1)[0]
            prefix = literal.rsplit('/', 1)[0] if '/' in literal else ''
            candidates = self.files_within(prefix)
        covered = sorted(
            d for d in self.directories if d and regex.fullmatch(d)
        )
        files: set[str] = set()
        for d in covered:
            if not any(_is_within(d, outer) for outer in covered if outer != d):
                files.update(self.files_within(d))
        if not rule.directory_only:
            files.update(f for f in candidates if regex.fullmatch(f))
        return files

    def directories_matching(self, segments: list[str]) -> list[str]:
        """Directories whose path is *segments* as an anchored glob."""
        if not segments:
            return ['']
        regex = re.compile(_codeowners_regex('/'.join(segments)))
        return [d for d in self.directories if d and regex.fullmatch(d)]

    def files_within(self, directory: str) -> list[str]:
        if not directory:
            return self.files
        # '0' sorts right after '/', so this is the range of `<directory>/`.
        return self.files[
            bisect.bisect_left(
                self.files, f'{directory}/'
            ) : bisect.bisect_left(self.files, f'{directory}0')
        ]


@dataclass(frozen=True)
class Placement:
    """A rule set in the `OWNERS` file of *directory*: for the whole
    directory when *glob* is None, else for the files *glob* matches."""

    rule: int
    directory: str
    glob: str | None


def place(rules: list[Rule], tree: Tree) -> list[Placement]:
    """Where each rule goes, before any rule hides another."""
    placements = []
    for rule in rules:
        regex = rule.regex()
        *dir_segments, last = rule.segments
        if not rule.anchored:
            dir_segments = ['**']
        if last == '*' and dir_segments and dir_segments[-1] == '**':
            # `x/**/*` is everything below the directories `x` matches.
            for directory in tree.directories_matching(dir_segments[:-1]):
                placements.append(Placement(rule.index, directory, None))
            continue

        whole = [d for d in tree.directories if d and regex.fullmatch(d)]
        placements.extend(Placement(rule.index, d, None) for d in whole)
        if rule.directory_only:
            continue

        matched = [
            f
            for f in tree.files
            if regex.fullmatch(f) and not any(_is_within(f, d) for d in whole)
        ]
        if dir_segments and dir_segments[-1] == '**':
            anchors = tree.directories_matching(dir_segments[:-1])
        else:
            anchors = tree.directories_matching(dir_segments)
        glob = last if _GLOB_CHARS.search(last) else _escape(last)
        for anchor in anchors:
            # A rule for the whole tree is kept for the files it will match.
            if not anchor or any(_is_within(f, anchor) for f in matched):
                placements.append(Placement(rule.index, anchor, glob))
    return placements


def arrange(
    placements: list[Placement], rules: list[Rule], tree: Tree
) -> list[Placement]:
    """Drops the placements a later rule for a whole directory hides, and
    repeats the later globs an earlier rule for a deeper directory would
    hide, so that the last matching rule wins as in `CODEOWNERS`."""
    whole = [p for p in placements if p.glob is None]
    kept = [
        p
        for p in placements
        if not any(
            q.rule > p.rule and _is_within(p.directory, q.directory)
            for q in whole
        )
    ]
    directory_rules = {p.directory: p.rule for p in kept if p.glob is None}
    regexes = {rule.index: rule.regex() for rule in rules}
    repeated = [
        Placement(q.rule, directory, q.glob)
        for q in kept
        if q.glob is not None
        for directory, rule in directory_rules.items()
        if rule < q.rule
        and directory != q.directory
        and _is_within(directory, q.directory)
        and any(
            regexes[q.rule].fullmatch(f) for f in tree.files_within(directory)
        )
    ]
    return kept + repeated


def build(
    placements: list[Placement], resolver: Resolver
) -> dict[str, Directory]:
    directories: dict[str, Directory] = {}
    for p in placements:
        directory = directories.setdefault(p.directory, Directory())
        if p.glob is None:
            directory.set_owners(resolver.grant(p.rule))
        else:
            directory.set_per_file(p.glob, resolver.grant(p.rule))
    return directories


def codeowners_owners(
    rules: list[Rule], tree: Tree, resolver: Resolver
) -> dict[str, Grant]:
    """Each file's owners under `CODEOWNERS`: the last matching rule's."""
    everyone = Grant((ALL_USERS,), frozenset({ALL_USERS}), len(rules))
    owners = dict.fromkeys(tree.files, everyone)
    for rule in rules:
        grant = resolver.grant(rule.index)
        for f in tree.files_matching(rule):
            owners[f] = grant
    return owners


@dataclass(frozen=True)
class Lookup:
    """What the `OWNERS` files give a file, and the `per-file` glob that
    decided it, if one did."""

    owners: frozenset[str]
    directory: str
    glob: str | None


def owners_lookup(path: str, directories: Mapping[str, Directory]) -> Lookup:
    """*path*'s owners as code-owners resolves them, for the files `build`
    writes."""
    found: set[str] = set()
    for directory in _ancestors(path):
        owners_file = directories.get(directory)
        if owners_file is None:
            continue
        relative = posixpath.relpath(path, directory) if directory else path
        matched = [
            (glob, grant)
            for glob, grant in owners_file.per_file.items()
            if _find_owners_regex(glob).fullmatch(relative)
        ]
        if matched:
            for _, grant in matched:
                found.update(grant.emails)
            glob = matched[0][0] if len(matched) == 1 else None
            return Lookup(frozenset(found), directory, glob)
        if owners_file.owners is not None:
            found.update(owners_file.owners.emails)
            return Lookup(frozenset(found), directory, None)
    return Lookup(frozenset(found or {ALL_USERS}), '', None)


def _agree(
    directory: str,
    regex: re.Pattern[str],
    owners: frozenset[str],
    wanted: Mapping[str, Grant],
    tree: Tree,
) -> bool:
    """Whether every file below *directory* that *regex* matches has
    *owners* under `CODEOWNERS`."""
    return all(
        wanted[f].emails == owners
        for f in tree.files_within(directory)
        if regex.fullmatch(posixpath.relpath(f, directory) if directory else f)
    )


def reconcile(
    directories: dict[str, Directory],
    wanted: Mapping[str, Grant],
    tree: Tree,
) -> None:
    """Overrides the owners of the files whose `OWNERS` owners differ from
    their `CODEOWNERS` ones, each as high up as the files it covers agree."""
    for _ in range(_MAX_ROUNDS):
        wrong = {
            f: lookup
            for f in tree.files
            if (lookup := owners_lookup(f, directories)).owners
            != wanted[f].emails
        }
        if not wrong:
            return
        fixed = set()
        for path, lookup in wrong.items():
            if path in fixed:
                continue
            glob = lookup.glob or _escape(posixpath.basename(path))
            directory = _parent(path)
            if lookup.glob and directory == lookup.directory:
                raise OwnersError(
                    f'{path} matches several per-file globs in '
                    f'{lookup.directory or "the root"} OWNERS; give it a '
                    'CODEOWNERS rule of its own'
                )
            regex = _find_owners_regex(glob)
            while (
                directory
                and (
                    lookup.glob is None
                    or _is_within(_parent(directory), lookup.directory)
                    and _parent(directory) != lookup.directory
                )
                and _agree(
                    _parent(directory), regex, wanted[path].emails, wanted, tree
                )
            ):
                directory = _parent(directory)
            owners_file = directories.setdefault(directory, Directory())
            # The overrides come last, with no comment of their own.
            override = wanted[path]
            owners_file.set_per_file(
                glob,
                Grant(override.lines, override.emails, len(wanted)),
            )
            prefix = f'{directory}/' if directory else ''
            fixed.update(
                f
                for f in wrong
                if f.startswith(prefix)
                and regex.fullmatch(f.removeprefix(prefix))
            )
    raise OwnersError(
        f'OWNERS files still differ from CODEOWNERS after {_MAX_ROUNDS} rounds'
    )


def generate(
    codeowners: str, files: Iterable[str], github: GitHubOwners
) -> dict[str, str]:
    """Every generated file's path and content."""
    rules = parse_codeowners(codeowners)
    tree = Tree(files)
    placements = arrange(place(rules, tree), rules, tree)
    resolver = Resolver(rules, github)
    directories = build(placements, resolver)
    reconcile(directories, codeowners_owners(rules, tree, resolver), tree)

    outputs = {
        posixpath.join(directory, 'OWNERS'): owners_file.render()
        for directory, owners_file in directories.items()
    }
    for team in sorted({t for r in rules for o in r.owners if (t := _team(o))}):
        outputs[team_list(team)] = github.render_list(team)
    _check_case_clashes(tree.files, outputs)
    return dict(sorted(outputs.items()))


def _check_case_clashes(files: Iterable[str], outputs: Iterable[str]) -> None:
    """Fails if a generated file and another path differ only in case, as
    on case-insensitive filesystems, such as macOS's, only one of them can
    be checked out."""
    names: dict[str, set[str]] = {}
    for path in (*files, *outputs):
        parts = path.split('/')
        for depth in range(1, len(parts) + 1):
            name = '/'.join(parts[:depth])
            names.setdefault(name.lower(), set()).add(name)
    clashes = sorted(
        sorted(paths) for paths in names.values() if len(paths) > 1
    )
    if clashes:
        raise OwnersError(
            'These paths differ only in case, so they cannot all be checked '
            'out on macOS: '
            + '; '.join(' and '.join(paths) for paths in clashes)
        )


def is_generated(path: str) -> bool:
    return posixpath.basename(path) == 'OWNERS' or (
        _parent(path) == TEAMS_DIR and path.endswith('_OWNERS')
    )


def tracked_files(root: Path) -> list[str]:
    output = subprocess.run(
        ['git', 'ls-files', '-z'],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [f for f in output.split('\0') if f]


def update_teams(
    github: GitHubOwners, codeowners: str, members: Callable[[str], list[str]]
) -> GitHubOwners:
    """*github* with the members *members* gives each team `CODEOWNERS`
    names. A member needs an email from one of the lists already."""
    teams = dict(github.teams)
    missing: dict[str, list[str]] = {}
    for team in sorted(
        {
            t
            for r in parse_codeowners(codeowners)
            for o in r.owners
            if (t := _team(o))
        },
        key=str.lower,
    ):
        logins = sorted(members(team), key=str.lower)
        if unknown := [login for login in logins if login not in github.users]:
            missing[team] = unknown
        teams[team] = logins
    if missing:
        raise OwnersError(
            'These members have no email in the lists yet; add '
            '"<email>  # @<login>" lines for them, then run again: '
            + '; '.join(
                f'{team_list(team)}: '
                + ', '.join(f'@{login}' for login in logins)
                for team, logins in missing.items()
            )
        )
    return GitHubOwners(github.users, teams)


def github_team_members(team: str) -> list[str]:
    """The logins of *team*'s members, from GitHub."""
    return subprocess.run(
        [
            'gh',
            'api',
            '--paginate',
            f'orgs/{ORG}/teams/{team}/members',
            '--jq',
            '.[].login',
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n', maxsplit=1)[0]
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--check',
        action='store_true',
        help='Fail if the OWNERS files differ from what CODEOWNERS gives.',
    )
    group.add_argument(
        '--update-teams',
        action='store_true',
        help=f'Refresh the team members in the {TEAMS_DIR} lists from GitHub.',
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[2]
    codeowners = (root / CODEOWNERS).read_text()
    files = tracked_files(root)
    try:
        github = GitHubOwners.load(root)
        if args.update_teams:
            github = update_teams(github, codeowners, github_team_members)
        outputs = generate(
            codeowners, (f for f in files if not is_generated(f)), github
        )
    except OwnersError as e:
        print(e, file=sys.stderr)
        return 1
    on_disk = set(files) | {
        f'{TEAMS_DIR}/{path.name}'
        for path in (root / TEAMS_DIR).glob('*_OWNERS')
    }
    stale = sorted(
        f
        for f in on_disk
        if is_generated(f) and f not in outputs and (root / f).is_file()
    )
    changed = sorted(
        path
        for path, content in outputs.items()
        if not (root / path).is_file() or (root / path).read_text() != content
    )

    if args.check:
        if not stale and not changed:
            return 0
        print(
            'The OWNERS files do not match .github/CODEOWNERS; run '
            f'{TEAMS_DIR}/generate_owners.py:',
            file=sys.stderr,
        )
        for path in changed:
            print(f'    {path}', file=sys.stderr)
        for path in stale:
            print(f'    {path} (stale)', file=sys.stderr)
        return 1

    for path in stale:
        (root / path).unlink()
    for path in changed:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(outputs[path])
    return 0


if __name__ == '__main__':
    sys.exit(main())
