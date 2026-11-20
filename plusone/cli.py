"""Chat with Plus One in the terminal, without iMessage (same brain, same audit trail).

    python -m plusone.cli            # interactive
    python -m plusone.cli "message"  # one message
"""
import sys

from . import brain, store

CHAT = "local-test"


def main() -> None:
    store.init()
    if len(sys.argv) > 1:
        print(brain.handle(CHAT, " ".join(sys.argv[1:])))
        return
    print("Plus One (terminal). Ctrl-C to quit.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text:
            print("\nplus one> " + brain.handle(CHAT, text))


if __name__ == "__main__":
    main()
