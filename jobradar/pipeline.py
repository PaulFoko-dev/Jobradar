"""Exécution d'une recherche : nettoyage → collecte → filtrage → dédoublonnage → enrichissement → analyse IA.

Une offre n'est marquée « analysée » que lorsque l'IA a renvoyé son analyse. Si l'analyse échoue
(quota atteint, délai dépassé, erreur), l'offre reste en attente et sera reprise à l'exécution suivante.
"""
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import ai, db, notifications
from .config import load_config
from .matching import UNKNOWN_CONTRACT, DedupIndex, dedup_key, relevance, scoring_order
from .sources import SOURCES
from .sources.common import polite_pause
from .sources.jobspy_src import fetch_linkedin_details
from .workflow import normalize_company

_lock = threading.Lock()
STATE = {"running": False, "search_id": None, "phase": "idle", "message": "", "progress": 0.0, "log": []}
PROVIDER_LABEL = {"claude": "Claude Code", "codex": "Codex"}


def _set(**kw):
    STATE.update(kw)


def _log(msg: str):
    line = f"{datetime.now():%H:%M:%S} {msg}"
    STATE["log"] = (STATE["log"] + [line])[-300:]
    print(line, flush=True)


def _enrich_linkedin(o: dict):
    """Lit la description et le logo d'une offre LinkedIn (absents des résultats de recherche)."""
    try:
        desc, logo = fetch_linkedin_details(o["url"])
        o["description"] = desc or o.get("description") or ""
        o["logo"] = o.get("logo") or logo
    except Exception:
        pass
    polite_pause(1)


def cleanup(search_id: int, crit: dict) -> DedupIndex:
    """Applique les règles de pertinence et de dédoublonnage aux offres déjà en base.
    Ne touche jamais aux offres suivies, en favori, ou jugées pertinentes par l'IA (score ≥ 50)."""
    offers = sorted(db.list_offers(search_id), key=lambda o: (o["score"] is None, -(o["score"] or 0)))
    index, removed, merged = DedupIndex(), 0, 0
    for o in offers:
        protected = bool(o.get("app_id")) or o["status"] == "fav" or (o["score"] or 0) >= 50
        reason = relevance(o, crit)
        if reason and reason != UNKNOWN_CONTRACT and not protected:
            db.delete_offer(o["id"])
            removed += 1
            continue
        found = index.find(o)
        if found and found[1] and not protected:
            db.merge_offer_into(found[1], o)
            merged += 1
            continue
        key = dedup_key(o)
        if found is None and key != o["dedup_key"]:
            db.set_dedup_key(o["id"], key)
        index.add(o, key if found is None else found[0], o["id"])
    if removed or merged:
        _log(f"Nettoyage : {removed} offres hors critères retirées, {merged} doublons fusionnés")
    return index


def _collect(search_id: int, crit: dict, cfg: dict, index: DedupIndex, stats: dict):
    known = {o["url"] for o in db.list_offers(search_id)}
    enabled = [s for s, on in cfg["sources"].items() if on and s in SOURCES]
    raw = []
    for i, name in enumerate(enabled):
        _set(message=f"Collecte : {name}", progress=0.5 * i / max(1, len(enabled)))
        try:
            raw += SOURCES[name](crit, cfg, known, _log)
        except Exception as e:
            stats["errors"] += 1
            _log(f"{name} : échec {type(e).__name__}: {e}")
    stats["collected"] = len(raw)

    _set(phase="dedup", message="Filtrage et dédoublonnage", progress=0.5)
    reasons = Counter()
    for o in raw:
        reason = relevance(o, crit)
        if reason == UNKNOWN_CONTRACT and "linkedin.com" in o["url"] and o["url"] not in known:
            _enrich_linkedin(o)  # le type de contrat est souvent dans la description
            reason = relevance(o, crit)
        if reason:
            reasons[reason] += 1
            continue
        o["links"] = [{"source": o["source"], "url": o["url"]}]
        if o.get("apply_url") and o["apply_url"] != o["url"]:
            o["links"].append({"source": "candidature directe", "url": o["apply_url"], "apply": True})
        found = index.find(o)
        o["dedup_key"] = found[0] if found else dedup_key(o)
        if not found:
            index.add(o, o["dedup_key"])
        if db.upsert_offer(search_id, o):
            stats["new"] += 1
    stats["filtered"] = sum(reasons.values())
    detail = ", ".join(f"{n} {r}" for r, n in reasons.most_common())
    _log(f"{stats['collected']} offres collectées, {stats['filtered']} écartées ({detail or 'aucune'}), "
         f"{stats['new']} nouvelles")


def _pending_scoring(search_id: int) -> list[dict]:
    """Offres à analyser, sans celles des entreprises exclues (économie de quota IA)."""
    excluded = db.excluded_company_keys()
    return [o for o in db.offers_to_score(search_id, 100_000)
            if normalize_company(o["company"] or "") not in excluded]


def _score(search_id: int, crit: dict, cfg: dict, stats: dict) -> list[dict]:
    """Analyse les offres en attente ; renvoie les offres analysées à cette exécution."""
    pending = _pending_scoring(search_id)
    todo = sorted(pending, key=lambda o: scoring_order(o, crit))[: cfg["max_offers_scored_per_run"]]
    if not todo:
        return []
    _set(phase="enrich", message="Lecture des descriptions manquantes", progress=0.55)
    for o in todo:
        if len(o["description"] or "") < 200 and "linkedin.com" in o["url"]:
            _enrich_linkedin(o)
            with db.connect() as c:
                c.execute("UPDATE offers SET description=?, logo=COALESCE(logo, ?) WHERE id=?",
                          (o["description"], o.get("logo"), o["id"]))

    label = PROVIDER_LABEL.get(cfg["ai_provider"], cfg["ai_provider"])
    size = cfg["scoring_batch_size"]
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]
    _set(phase="score", message=f"Analyse par {label} : 0/{len(todo)} offres", progress=0.6)
    _log(f"Analyse de {len(todo)} offres par {label} ({len(batches)} lots, {len(pending) - len(todo)} reportées)")
    done, quota_hit, analyzed = 0, False, []
    with ThreadPoolExecutor(max_workers=cfg["scoring_parallel"]) as pool:
        futures = {pool.submit(ai.score_offers, crit, b): b for b in batches}
        for f in as_completed(futures):
            batch = futures[f]
            done += len(batch)
            if f.cancelled():
                continue
            try:
                by_id = {o["id"]: o for o in batch}
                for res in f.result():
                    if res["id"] in by_id:
                        res["score"] = max(0, min(100, int(res["score"])))
                        db.save_analysis(res["id"], res)
                        stats["scored"] += 1
                        analyzed.append(by_id[res["id"]] | res)
            except ai.QuotaError as e:
                stats["errors"] += 1
                if not quota_hit:
                    quota_hit = True
                    _log(f"{e} — arrêt de l'analyse, les offres restantes seront reprises à la prochaine exécution")
                    for other in futures:
                        other.cancel()  # les lots pas encore démarrés ne partent pas
            except Exception as e:
                stats["errors"] += 1
                _log(f"Analyse d'un lot échouée (reprise à la prochaine exécution) : {e}")
            _set(message=f"Analyse par {label} : {done}/{len(todo)} offres", progress=0.6 + 0.4 * done / len(todo))
    stats["pending"] = len(_pending_scoring(search_id))
    return analyzed


def log_email(result: dict, log=None) -> None:
    """Journalise le résultat de ``notifications.deliver_email_digest`` (aucun secret dans le message)."""
    log = log or _log
    if result["sent"]:
        log(f"E-mail récapitulatif envoyé ({result['sent']} alerte(s))")
    elif result["error"]:
        log(f"E-mail non envoyé : {result['error']} Nouvel essai à la prochaine exécution.")


def _notify(analyzed: list[dict], send_email: bool = True):
    """Alertes et rappels de fin d'exécution ; un échec ici ne compromet jamais la recherche."""
    try:
        created = notifications.alert_high_score_offers(analyzed)
        if created:
            _log(f"{len(created)} offre(s) très pertinente(s) signalée(s)")
            notifications.deliver_desktop(created, notifications.offers_desktop_text(created))
        notifications.refresh_notifications()
        if send_email:
            log_email(notifications.deliver_email_digest())
    except Exception as e:
        _log(f"Notifications : échec {type(e).__name__}: {e}")


def _auto_apply(search_id: int) -> None:
    """Auto-candidature de fin d'exécution ; un échec ici ne compromet jamais la recherche."""
    try:
        from . import preparation
        scheduled = preparation.schedule(search_id)
        if scheduled:
            _log(f"Auto-candidature : {len(scheduled)} offre(s) confiée(s) à la préparation automatique")
    except Exception as e:
        _log(f"Auto-candidature : échec {type(e).__name__}: {e}")


def run_search(search_id: int, score_only: bool = False, *, send_email: bool = True) -> dict:
    """Exécute une recherche complète. Une seule exécution à la fois.

    ``send_email=False`` laisse l'appelant envoyer un seul récapitulatif pour
    plusieurs recherches (``cli refresh``).
    """
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Une recherche est déjà en cours.")
    run_id = None
    stats = {"collected": 0, "filtered": 0, "new": 0, "scored": 0, "pending": 0, "errors": 0}
    try:
        _set(running=True, search_id=search_id, phase="collect", progress=0.0, log=[], message="Démarrage…")
        search = db.get_search(search_id)
        if not search:
            raise RuntimeError("Recherche introuvable")
        crit = search["criteria"]
        cfg = load_config()
        run_id = db.start_run(search_id)

        index = cleanup(search_id, crit)
        if not score_only:
            _collect(search_id, crit, cfg, index, stats)
        analyzed = _score(search_id, crit, cfg, stats)

        db.mark_search_run(search_id)
        _notify(analyzed, send_email)
        _auto_apply(search_id)
        _log(f"Terminé : {stats['new']} nouvelles offres, {stats['scored']} analysées, "
             f"{stats['pending']} en attente d'analyse")
        _set(phase="done", message="Terminé", progress=1.0)
        db.finish_run(run_id, "done", stats, "\n".join(STATE["log"]))
        return stats
    except Exception as e:
        _log(f"Erreur : {e}")
        traceback.print_exc()
        _set(phase="error", message=str(e))
        if run_id:
            db.finish_run(run_id, "error", stats, "\n".join(STATE["log"]))
        raise
    finally:
        STATE["running"] = False
        _lock.release()


def run_in_background(search_id: int, score_only: bool = False):
    if STATE["running"]:
        raise RuntimeError("Une recherche est déjà en cours.")

    def target():
        try:
            run_search(search_id, score_only)
        except Exception:
            pass

    threading.Thread(target=target, daemon=True).start()
