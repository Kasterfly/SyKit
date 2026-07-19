from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments and arguments[0].lower() == "package":
        import package

        return 0 if package.run(arguments[1:]) else 1
    if arguments and arguments[0].lower() == "keys":
        import keys

        return 0 if keys.run(arguments[1:]) else 1
    if arguments and arguments[0].lower() == "update":
        import update

        return 0 if update.run(arguments[1:]) else 1
    if arguments and arguments[0].lower() == "build":
        flags = [argument.lower() for argument in arguments[1:]]
        if any(flag != "--dev" for flag in flags):
            import help

            help.print_help()
            return 1
        import build

        return 0 if build.run(dev="--dev" in flags) else 1
    if len(arguments) != 1:
        import help

        help.print_help()
        return 0

    action = arguments[0].lower()
    if action == "help":
        import help

        help.print_help()
        return 0
    if action in {"version", "--version", "-v"}:
        from sykit import __version__

        print(f"SyKit {__version__} (beta)")
        return 0
    if action == "init":
        import init

        return 0 if init.run() else 1
    import help

    help.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
