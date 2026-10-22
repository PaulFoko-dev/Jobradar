"""Ligne de commande.

    python -m jobradar.cli serve [--open]   # lance l'interface web (et l'ouvre dans le navigateur)
    python -m jobradar.cli refresh          # exécute toutes les recherches quotidiennes (pour le timer)
    python -m jobradar.cli refresh --id 3   # exécute une recherche précise
"""
import argparse
import subprocess
import sys

from . import db, pipeline


def main():
    p = argparse.ArgumentParser(prog="jobradar")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="ouvrir le navigateur")
    r = sub.add_parser("refresh")
    r.add_argument("--id", type=int)
    args = p.parse_args()

    db.init()
    if args.cmd == "serve":
        import threading
        import webbrowser

        import uvicorn

        if args.open:
            threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{args.port}"]).start()
        uvicorn.run("jobradar.server:app", host="127.0.0.1", port=args.port)
    else:
        targets = [db.get_search(args.id)] if args.id else [
            s for s in db.list_searches() if s["frequency"] == "daily"
        ]
        total_new = 0
        for s in filter(None, targets):
            print(f"== {s['name']}")
            total_new += pipeline.run_search(s["id"])["new"]
        if total_new:
            notify(f"{total_new} nouvelles offres trouvées")


def notify(message: str):
    """Notification de bureau (Linux : notify-send ; macOS : osascript ; ailleurs : rien)."""
    cmds = {
        "linux": ["notify-send", "JobRadar", message],
        "darwin": ["osascript", "-e", f'display notification "{message}" with title "JobRadar"'],
    }
    cmd = cmds.get(sys.platform)
    if cmd:
        try:
            subprocess.run(cmd, check=False)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
