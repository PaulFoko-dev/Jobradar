"""Ligne de commande.

    python -m jobradar.cli serve [--open]   # lance l'interface web (et l'ouvre dans le navigateur)
    python -m jobradar.cli refresh          # exécute toutes les recherches quotidiennes (pour le timer)
    python -m jobradar.cli refresh --id 3   # exécute une recherche précise
    python -m jobradar.cli backup           # sauvegarde ZIP dans data/sauvegardes/
    python -m jobradar.cli navigateur       # ouvre le navigateur dédié aux connexions
    python -m jobradar.cli restaurer F.zip  # restaure une sauvegarde dans un dossier de données vide

``refresh`` entretient aussi les archives des candidatures : rattrapage des dossiers manquants,
captures en attente et vérification (au plus quotidienne) des annonces retirées.
"""
import argparse
import logging

from . import archive, config, db, export, notifications, pipeline, preparation
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
    sub.add_parser("navigateur", help="ouvre le navigateur dédié aux candidatures")
    restore = sub.add_parser("restaurer", help="restaure une sauvegarde ZIP dans un dossier de données vide")
    restore.add_argument("zip", help="fichier JobRadar-sauvegarde-….zip")
    args = p.parse_args()

    if args.cmd == "restaurer":
        from pathlib import Path

        result = export.restore_backup(Path(args.zip), data_dir=config.DATA_DIR, archive_dir=config.archive_dir())
        print(f"{result['files']} fichier(s) restauré(s) dans {config.DATA_DIR}."
              + (" Données chiffrées : lance JobRadar et saisis ta clé de récupération." if result["encrypted"] else ""))
        return
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
    elif args.cmd == "navigateur":
        from . import application_browser
        try:
            browser = application_browser.open_system_browser()
        except application_browser.BrowserUnavailable as exc:
            p.error(str(exc))
        print(f"{browser['name']} (profil JobRadar) est ouvert : connecte-toi une fois à tes comptes, "
              "puis laisse JobRadar le réutiliser.")
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
        try:
            from . import inbox

            found = inbox.scan()
            if found.get("enabled"):
                print(f"Boîte de réception : {found['proposals']} réponse(s) proposée(s), "
                      f"{found['confirmed']} envoi(s) confirmé(s)")
        except Exception as exc:  # boîte injoignable : l'actualisation continue
            print(f"Boîte de réception : lecture impossible ({exc})")
        try:
            from . import private_io

            sealed = private_io.seal_tree()  # captures et PDF écrits directement par le navigateur
            if sealed:
                print(f"Coffre : {sealed} fichier(s) chiffré(s)")
        except Exception as exc:  # coffre fermé : rien n'est perdu, le balayage reprendra
            print(f"Coffre : balayage impossible ({exc})")
        # Sans serveur, les préparations (dont l'auto-candidature) sont faites ici ; les envois assistés
        # qu'elles placent dans la file partiront au prochain démarrage du serveur.
        prepared = preparation.run_pending()
        if prepared:
            print(f"Préparations de documents : {prepared} traitée(s)")


if __name__ == "__main__":
    main()
