"""Migrations SQLite versionnées de JobRadar.

SQLite expose ``PRAGMA user_version`` dans chaque fichier de base. Nous
l'utilisons plutôt qu'une table métier afin que la version reste disponible
même pendant les opérations de reconstruction de tables.

La série de migrations est exécutée dans une transaction ``IMMEDIATE``. Le
verrou est acquis *avant* de lire la version : deux processus qui démarrent en
même temps ne peuvent donc jamais prendre une décision depuis la même version
périmée. La version n'est avancée qu'après le succès complet de la série.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = tuple[int, Callable[[sqlite3.Connection], None]]


def _create_initial_schema(conn: sqlite3.Connection) -> None:
    """Crée le schéma historique sans écraser une base existante."""
    statements = (
        """CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            form_json TEXT NOT NULL DEFAULT '{}', prompt TEXT NOT NULL DEFAULT '',
            criteria_json TEXT NOT NULL DEFAULT '{}',
            frequency TEXT NOT NULL DEFAULT 'daily', created_at TEXT NOT NULL,
            last_run TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY,
            search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
            dedup_key TEXT NOT NULL, title TEXT NOT NULL, company TEXT,
            location TEXT, contract TEXT, date_posted TEXT, description TEXT,
            url TEXT NOT NULL, links_json TEXT NOT NULL DEFAULT '[]',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            analysis_json TEXT, score INTEGER, in_zone INTEGER,
            status TEXT NOT NULL DEFAULT 'new', viewed INTEGER NOT NULL DEFAULT 0,
            UNIQUE(search_id, dedup_key)
        )""",
        """CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY, offer_id INTEGER UNIQUE, title TEXT NOT NULL,
            company TEXT, location TEXT, url TEXT, score INTEGER,
            status TEXT NOT NULL DEFAULT 'consultee', notes TEXT NOT NULL DEFAULT '',
            history_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, search_id INTEGER NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            stats_json TEXT NOT NULL DEFAULT '{}', log TEXT NOT NULL DEFAULT ''
        )""",
    )
    for statement in statements:
        conn.execute(statement)


def _add_logo_columns(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes logo introduites après le premier schéma."""
    for table in ("offers", "applications"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "logo" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN logo TEXT")


def _application_offer_foreign_key(conn: sqlite3.Connection) -> None:
    """Reconstruit ``applications`` avec un lien sûr vers ``offers``.

    Les anciennes bases n'avaient pas de clé étrangère. Un éventuel identifiant
    déjà orphelin est converti en ``NULL`` ; toutes les données autonomes de la
    candidature (titre, notes et historique notamment) sont conservées.
    """
    foreign_keys = conn.execute("PRAGMA foreign_key_list(applications)").fetchall()
    if any(row[2] == "offers" and row[3] == "offer_id" and row[6].upper() == "SET NULL"
           for row in foreign_keys):
        return

    conn.execute(
        """CREATE TABLE applications_new (
            id INTEGER PRIMARY KEY,
            offer_id INTEGER UNIQUE REFERENCES offers(id) ON DELETE SET NULL,
            title TEXT NOT NULL, company TEXT, location TEXT, url TEXT,
            score INTEGER, logo TEXT, status TEXT NOT NULL DEFAULT 'consultee',
            notes TEXT NOT NULL DEFAULT '', history_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """INSERT INTO applications_new(
               id, offer_id, title, company, location, url, score, logo, status,
               notes, history_json, created_at, updated_at
           )
           SELECT a.id,
                  CASE WHEN o.id IS NULL THEN NULL ELSE a.offer_id END,
                  a.title, a.company, a.location, a.url, a.score, a.logo,
                  a.status, a.notes, a.history_json, a.created_at, a.updated_at
           FROM applications AS a
           LEFT JOIN offers AS o ON o.id = a.offer_id"""
    )
    conn.execute("DROP TABLE applications")
    conn.execute("ALTER TABLE applications_new RENAME TO applications")


def _add_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """Ajoute une colonne seulement si elle n'existe pas déjà.

    Cette garde rend chaque étape tolérante à une ancienne installation ayant
    reçu manuellement une partie du schéma avant l'introduction des versions.
    ``table`` et ``name`` proviennent exclusivement du code des migrations.
    """
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _offer_timing(conn: sqlite3.Connection) -> None:
    """Ajoute les dates normalisées sans interpréter les anciennes valeurs brutes."""
    _add_column(conn, "offers", "published_at_utc", "TEXT")
    _add_column(
        conn,
        "offers",
        "application_deadline_date",
        "TEXT CHECK (application_deadline_date IS NULL OR "
        "(length(application_deadline_date) = 10 AND date(application_deadline_date) = application_deadline_date))",
    )
    _add_column(conn, "offers", "application_deadline_at_utc", "TEXT")
    _add_column(conn, "offers", "application_deadline_timezone", "TEXT")
    _add_column(conn, "offers", "deadline_raw", "TEXT")
    _add_column(conn, "offers", "deadline_source", "TEXT")
    _add_column(conn, "offers", "deadline_extracted_at_utc", "TEXT")
    _add_column(
        conn,
        "offers",
        "deadline_confidence",
        "REAL CHECK (deadline_confidence IS NULL OR deadline_confidence BETWEEN 0.0 AND 1.0)",
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_deadline_date ON offers(application_deadline_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_published_at ON offers(published_at_utc)")


def _company_rules(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_rules (
            id INTEGER PRIMARY KEY,
            company_key TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            disposition TEXT NOT NULL DEFAULT 'neutral'
                CHECK (disposition IN ('neutral', 'favorite', 'excluded')),
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )"""
    )


def _application_follow_up(conn: sqlite3.Connection) -> None:
    _add_column(conn, "applications", "applied_at_utc", "TEXT")
    _add_column(conn, "applications", "follow_up_snoozed_until_utc", "TEXT")
    _add_column(
        conn,
        "applications",
        "follow_up_disabled",
        "INTEGER NOT NULL DEFAULT 0 CHECK (follow_up_disabled IN (0, 1))",
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'status_changed', 'follow_up_draft_created', 'follow_up_sent',
                'response_received', 'interview_scheduled', 'note'
            )),
            occurred_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_events_timeline
           ON application_events(application_id, occurred_at_utc DESC)"""
    )


def _interviews(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interviews (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            uid TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            starts_at_utc TEXT NOT NULL,
            ends_at_utc TEXT NOT NULL,
            timezone TEXT NOT NULL,
            location TEXT,
            meeting_url TEXT,
            notes TEXT NOT NULL DEFAULT '',
            preparation_path TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'cancelled')),
            reminder_minutes INTEGER NOT NULL DEFAULT 1440 CHECK (reminder_minutes >= 0),
            sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            CHECK (ends_at_utc > starts_at_utc)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_interviews_application_start
           ON interviews(application_id, starts_at_utc)"""
    )


def _notifications(conn: sqlite3.Connection) -> None:
    """Ajoute une boîte locale durable ; aucun envoi externe n'est effectué ici."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            dedupe_key TEXT NOT NULL UNIQUE,
            offer_id INTEGER REFERENCES offers(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'delivered', 'dismissed', 'failed')),
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at_utc TEXT NOT NULL,
            delivered_at_utc TEXT,
            error TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_status_created "
        "ON notifications(status, created_at_utc DESC)"
    )


def _notification_email(conn: sqlite3.Connection) -> None:
    """Suivi de l'envoi par e-mail : date d'envoi, tentatives échouées et dernière erreur."""
    _add_column(conn, "notifications", "emailed_at_utc", "TEXT")
    _add_column(conn, "notifications", "email_attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "notifications", "email_error", "TEXT")


def _application_archive(conn: sqlite3.Connection) -> None:
    """Archive locale de chaque candidature : dossier, état de la capture et instantané figé de l'offre."""
    _add_column(conn, "applications", "archive_id", "TEXT")
    _add_column(conn, "applications", "archive_dir", "TEXT")
    _add_column(
        conn,
        "applications",
        "capture_status",
        "TEXT CHECK (capture_status IS NULL OR capture_status IN ('pending', 'running', 'done', 'failed'))",
    )
    _add_column(conn, "applications", "capture_level", "TEXT")
    _add_column(conn, "applications", "capture_error", "TEXT")
    _add_column(conn, "applications", "captured_at_utc", "TEXT")
    _add_column(conn, "applications", "offer_online", "INTEGER CHECK (offer_online IS NULL OR offer_online IN (0, 1))")
    _add_column(conn, "applications", "offer_checked_at_utc", "TEXT")
    _add_column(conn, "applications", "offer_snapshot_json", "TEXT")
    # SQLite refuse ADD COLUMN ... UNIQUE : l'unicité passe par un index.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_archive_id ON applications(archive_id)")


def _application_documents(conn: sqlite3.Connection) -> None:
    """Versions rendues des CV et lettres d'une candidature."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('cv', 'lettre')),
            version INTEGER NOT NULL CHECK (version >= 1),
            composition_json TEXT NOT NULL,
            template_json TEXT NOT NULL,
            ats_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'brouillon'
                CHECK (status IN ('brouillon', 'pret', 'envoye')),
            files_json TEXT NOT NULL DEFAULT '[]',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            UNIQUE(application_id, kind, version)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_documents_application_version
           ON documents(application_id, version DESC, kind)"""
    )


def _application_attempts(conn: sqlite3.Connection) -> None:
    """Sessions d'envoi : choix exacts, mode et état durable de chaque tentative."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS application_attempts (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('manual', 'assisted')),
            status TEXT NOT NULL CHECK (status IN (
                'manual_open', 'queued', 'running', 'needs_user', 'submitted', 'failed', 'cancelled'
            )),
            cv_document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            letter_document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            target_url TEXT NOT NULL,
            provider TEXT NOT NULL,
            answers_json TEXT NOT NULL DEFAULT '[]',
            intervention_json TEXT,
            error TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            submitted_at_utc TEXT,
            CHECK (cv_document_id IS NOT NULL OR letter_document_id IS NOT NULL)
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_attempts_queue
           ON application_attempts(status, created_at_utc, id)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_attempts_application
           ON application_attempts(application_id, created_at_utc DESC, id DESC)"""
    )


def _application_review_policy(conn: sqlite3.Connection) -> None:
    """Fige la politique de validation choisie pour chaque envoi assisté.

    Le choix est stocké sur la tentative, et non seulement dans la configuration
    globale : modifier les préférences plus tard ne peut donc pas changer le
    comportement d'une candidature déjà placée dans la file.
    """
    _add_column(
        conn,
        "application_attempts",
        "review_policy",
        "TEXT NOT NULL DEFAULT 'first_five' "
        "CHECK (review_policy IN ('first_five', 'always', 'never'))",
    )


def _interview_progression(conn: sqlite3.Connection) -> None:
    """Préparations, simulations et contacts de l'étape 4, tous rattachés à une candidature."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interview_preparations (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            interview_id INTEGER REFERENCES interviews(id) ON DELETE SET NULL,
            content_json TEXT NOT NULL,
            markdown_path TEXT,
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_interview_preparations_application
           ON interview_preparations(application_id, created_at_utc DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interview_simulations (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            preparation_id INTEGER REFERENCES interview_preparations(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed')),
            current_question INTEGER NOT NULL DEFAULT 0 CHECK (current_question >= 0),
            questions_json TEXT NOT NULL,
            turns_json TEXT NOT NULL DEFAULT '[]',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_interview_simulations_application
           ON interview_simulations(application_id, created_at_utc DESC, id DESC)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS application_contacts (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            role TEXT,
            company TEXT,
            email TEXT,
            phone TEXT,
            profile_url TEXT,
            context TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_application_contacts_application
           ON application_contacts(application_id, name, id)"""
    )


def _application_submit_guard(conn: sqlite3.Connection) -> None:
    """Mémorise le clic sur le bouton d'envoi : après lui, aucune reprise automatique n'est permise.

    Une candidature peut être partie même si sa confirmation n'a pas été reconnue ;
    la renvoyer créerait un doublon chez le recruteur.
    """
    _add_column(conn, "application_attempts", "submit_clicked_at_utc", "TEXT")


MIGRATIONS: tuple[Migration, ...] = (
    (1, _create_initial_schema),
    (2, _add_logo_columns),
    (3, _application_offer_foreign_key),
    (4, _offer_timing),
    (5, _company_rules),
    (6, _application_follow_up),
    (7, _interviews),
    (8, _notifications),
    (9, _notification_email),
    (10, _application_archive),
    (11, _application_documents),
    (12, _application_attempts),
    (13, _application_review_policy),
    (14, _interview_progression),
    (15, _application_submit_guard),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(conn: sqlite3.Connection) -> None:
    """Applique dans l'ordre les migrations absentes de ``conn``.

    Une version plus récente que celle comprise par cette application est
    refusée : démarrer avec un ancien binaire pourrait autrement altérer une
    base créée par une version future de JobRadar.
    """
    # La lecture de user_version fait partie de la section critique. La placer
    # avant BEGIN IMMEDIATE introduirait un TOCTOU : un autre processus pourrait
    # migrer la base entre la lecture et l'acquisition du verrou.
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > LATEST_SCHEMA_VERSION:
            raise RuntimeError(
                f"Base JobRadar en version {current}, version maximale prise en charge : "
                f"{LATEST_SCHEMA_VERSION}"
            )

        for version, migration in MIGRATIONS:
            if version <= current:
                continue
            migration(conn)
            conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
