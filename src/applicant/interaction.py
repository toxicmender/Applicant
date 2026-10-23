"""The few moments a source has to ask the person at the keyboard something.

A `--login` run opens a visible browser and waits while someone clears a bot
check; LinkedIn's two-factor sign in needs a one-time code. Those used to be
`print` and `input` calls inside the adapters, which put text on the stdout of
any program embedding the library. Now an adapter asks an `Interaction`, and
the caller decides how - the CLI answers on the terminal, a test answers from a
script, and a program with no person present can refuse.

`Terminal` is the default: the question goes to stderr, so a piped stdout still
carries only the command's answer, and the reply comes from stdin.
"""

from __future__ import annotations

import getpass
import sys
from typing import Protocol


class Interaction(Protocol):
    def pause(self, message: str) -> None:
        """Say what the person needs to do, and wait until they have done it."""
        ...

    def ask(self, question: str, *, secret: bool = False) -> str:
        """A line of text from the person; `secret` keeps it off the screen."""
        ...


class Terminal:
    """Questions on stderr, answers from stdin."""

    def pause(self, message: str) -> None:
        sys.stderr.write(f'{message}\n')
        sys.stderr.flush()
        input()

    def ask(self, question: str, *, secret: bool = False) -> str:
        if secret:
            return getpass.getpass(question, stream=sys.stderr)
        sys.stderr.write(question)
        sys.stderr.flush()
        return input()


class Scripted:
    """Answers given in advance, for tests and unattended runs.

    `answers` are handed out in order to `ask`; `pause` returns at once. Every
    message is kept in `said`, so a test can check what the person was told.
    Running out of answers is an error rather than a hang.
    """

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.said: list[str] = []

    def pause(self, message: str) -> None:
        self.said.append(message)

    def ask(self, question: str, *, secret: bool = False) -> str:
        del secret
        self.said.append(question)
        if not self.answers:
            raise RuntimeError(f'no scripted answer left for {question!r}')
        return self.answers.pop(0)
