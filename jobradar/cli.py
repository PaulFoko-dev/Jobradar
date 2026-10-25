"""Ligne de commande.

    python -m jobradar.cli serve [--open]   # lance l'interface web (et l'ouvre dans le navigateur)
    python -m jobradar.cli refresh          # exécute toutes les recherches quotidiennes (pour le timer)
    python -m jobradar.cli refresh --id 3   # exécute une recherche précise
    python -m jobradar.cli backup           # sauvegarde ZIP dans data/sauvegardes/

``refresh`` entretient aussi les archives des candidatures : rattrapage des dossiers manquants,
captures en attente et vérification (au plus quotidienne) des annonces retirées.
"""
import argparse
import logging

from . import archive, config, db, export, notifications, pipeline
from .config import settings


def maintain_archives() -> None:
    """Un échec des archives ne doit pas faire échouer l'exécution programmée."""
    try:
        created = archive.catch_up()
        captured = archive.run_pending_captures()
        online = archive.check_offers_online()
    except Exception as exc:
        logging.getLogger(__name__).exception("Archives des candidatures")
        print(f"Archives : échec ({exc})")
        return
    print(f"Archives : {len(created)} dossier(s) créé(s), {captured} capture(s), "
          f"{online['checked']} annonce(s) vérifiée(s)")


def main():
    p = argparse.ArgumentParser(prog="jobradar")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="ouvrir le navigateur")
    r = sub.add_parser("refresh")
    r.add_argument("--id", type=int)
    sub.add_parser("backup", help="sauvegarde ZIP : base, configuration et archives des candidatures")
    args = p.parse_args()

    db.init()
    if args.cmd == "serve":
        import threading
        import webbrowser

        import uvicorn

        if args.open:
            threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{args.port}"]).start()
        uvicorn.run("jobradar.server:app", host="127.0.0.1", port=args.port)
    elif args.cmd == "backup":
        result = export.backup()
        print(f"Sauvegarde créée : {config.DATA_DIR / 'sauvegardes' / result['path']} "
              f"({result['size_bytes'] / 1_000_000:.1f} Mo)")
    else:
        targets = [db.get_search(args.id)] if args.id else [
            s for s in db.list_searches() if s["frequency"] == "daily"
        ]
        total_new = 0
        for s in filter(None, targets):
            print(f"== {s['name']}")
            total_new += pipeline.run_search(s["id"], send_email=False)["new"]
        if total_new and settings().alert_desktop:
            notifications.notify(f"{total_new} nouvelles offres trouvées")
        # Rappels de relance et d'entretien, même si aucune recherche n'est programmée.
        notifications.refresh_notifications()
        notifications.deliver_pending_reminders()
        # Un seul e-mail récapitulatif pour toutes les recherches de l'exécution.
        pipeline.log_email(notifications.deliver_email_digest(), print)
        maintain_archives()


if __name__ == "__main__":
    main()
