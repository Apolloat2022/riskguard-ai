"""Verify the committed lockfiles still satisfy pyproject's declared ranges.

Run against an environment installed *from the lock*: it reads every direct
requirement out of pyproject.toml and checks the installed version against that
requirement's specifier.

**Why this, and not "regenerate and diff".** A regenerate-and-diff check fails
whenever anything upstream publishes a release, because a fresh resolution
legitimately picks up newer versions. That is time passing, not a defect, and a
check that cries wolf on unrelated PRs gets disabled within a month.

What actually needs catching is a lock that has fallen out of step with the
constraints beside it — someone tightens `mcp>=1.9` to `mcp>=1.9,<2` and forgets
to regenerate, or edits a lock by hand into violating a bound. That is
deterministic, and it is what this checks.

It deliberately does *not* check for newer upstream releases. Picking those up
is a deliberate act (regenerate, diff the resulting image, deploy), not
something CI should nag about.
"""

from __future__ import annotations

import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.requirements import Requirement

# The extras the runtime lock covers. Kept in step with the Dockerfile's
# regeneration command and the README.
RUNTIME_EXTRAS = ("ml", "api", "agent", "mcp")


def direct_requirements(pyproject: dict, extras: tuple[str, ...]) -> list[Requirement]:
    """Base dependencies plus the named extras, as parsed Requirements."""
    project = pyproject["project"]
    raw = list(project.get("dependencies", []))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"pyproject declares no '{extra}' extra")
        raw.extend(optional[extra])
    return [Requirement(item) for item in raw]


def check(extras: tuple[str, ...]) -> list[str]:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    problems: list[str] = []

    for requirement in direct_requirements(pyproject, extras):
        # Markers gate a requirement to certain platforms/pythons; one that
        # does not apply here is correctly absent from the environment.
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            problems.append(f"{requirement.name}: declared in pyproject but not installed")
            continue
        if installed not in requirement.specifier:
            problems.append(
                f"{requirement.name}: installed {installed} violates "
                f"'{requirement}' declared in pyproject"
            )
    return problems


def _pins(path: Path) -> dict[str, str]:
    """name -> version from a `uv pip compile` output file."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, pinned = line.partition("==")
        pins[name.strip().lower().replace("_", "-")] = pinned.strip()
    return pins


def check_locks_agree(runtime: Path, dev: Path) -> list[str]:
    """The runtime lock must be a subset of the dev lock at identical versions.

    The two files are generated separately, so regenerating one and forgetting
    the other is easy and silent — CI would then test against versions the
    image never installs, which is the failure this whole exercise is about.
    Comparing them costs nothing and needs no second install.
    """
    problems: list[str] = []
    runtime_pins, dev_pins = _pins(runtime), _pins(dev)

    for name, pinned in sorted(runtime_pins.items()):
        dev_pinned = dev_pins.get(name)
        if dev_pinned is None:
            problems.append(f"{name}=={pinned} is in {runtime.name} but missing from {dev.name}")
        elif dev_pinned != pinned:
            problems.append(
                f"{name}: {runtime.name} pins {pinned}, {dev.name} pins {dev_pinned}"
            )
    return problems


def main() -> int:
    extras = tuple(sys.argv[1:]) or RUNTIME_EXTRAS
    problems = check(extras)

    runtime_lock, dev_lock = Path("requirements.lock"), Path("requirements-dev.lock")
    if runtime_lock.exists() and dev_lock.exists():
        problems.extend(check_locks_agree(runtime_lock, dev_lock))

    if problems:
        print(f"Lock is out of step with pyproject for extras {', '.join(extras)}:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nRegenerate the lockfiles (see README > Dependencies) and commit the result.\n"
            "The generation must run on linux/amd64; a lock built on another\n"
            "platform resolves different markers and is not installable in the image."
        )
        return 1

    print(f"Lock satisfies every pyproject constraint for extras: {', '.join(extras)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
