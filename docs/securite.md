# Sécurité et confidentialité des données

JobRadar tourne entièrement sur ton ordinateur. Ce document décrit ce qui est protégé, comment, et ce qui ne peut
pas l'être.

## Modèle de menace

| Menace | Protection |
|---|---|
| Vol ou perte de l'ordinateur, copie du dossier JobRadar | Données privées chiffrées au repos (coffre AES-256-GCM) |
| Sauvegarde ZIP envoyée ou égarée | Contenu privé déjà chiffré dans le ZIP ; restauration impossible sans la clé |
| Page web malveillante qui appelle l'API locale | Requêtes modifiantes refusées si `Host` ou `Origin` n'est pas local |
| DNS rebinding | Même contrôle `Host` (127.0.0.1, localhost, ::1 uniquement) |
| Fuite vers l'assistant IA | Contrat aveugle : aucune donnée privée dans un prompt (voir `ia-contrat.md`) |
| Formulaire de candidature piégé (injection de consignes) | Actions IA limitées à un ensemble fermé et validées localement |

Hors périmètre : un logiciel malveillant qui s'exécute avec ton compte pendant que JobRadar est déverrouillé.

## Connexion à l'application

- Compte local unique : identifiant et mot de passe haché avec scrypt (sel aléatoire), dans `data/compte.json` (0600).
- Session : cookie signé HMAC-SHA256 (identifiant et expiration, 30 jours), HttpOnly et SameSite=Strict. Changer de
  mot de passe renouvelle la clé de session et déconnecte les autres sessions.
- Dès que le compte existe, toute route `/api` exige une session ; 5 échecs en 5 minutes suspendent les essais.
- La mise à jour quotidienne (`cli refresh`) n'utilise pas l'API HTTP et n'est pas concernée.
- Ce mot de passe protège l'accès à l'interface ; le chiffrement des données repose sur le coffre (clé de
  récupération), indépendant du mot de passe.

## Classification des données

- **Chiffrées** : profil (`data/profil/`, versions, sessions, documents de base), archives `candidatures/` (PDF,
  captures, journaux du robot, DOM enregistrés), colonnes privées de la base (notes, historique, réponses de
  formulaire, interventions, compositions de documents, contacts, entretiens, notifications, préparations).
- **En clair** : offres publiques, recherches, états, identifiants et dates techniques nécessaires aux index.
- **Jamais mémorisées** : réponses sensibles (santé, handicap, origine, genre, religion, casier), toujours redemandées.

## Format du coffre

- Une **clé de données** (256 bits, aléatoire) chiffre tout, en AES-256-GCM avec un nonce aléatoire de 96 bits.
- Elle est enveloppée deux fois dans `data/vault.json` (qui ne contient aucune clé en clair) :
  - par la **clé principale**, rangée dans le trousseau du système (ou la variable `JOBRADAR_VAULT_KEY` pour les tests
    et déploiements automatisés) ;
  - par la **clé de récupération**, 256 bits affichés une seule fois en groupes de 4 caractères, avec somme de contrôle.
- Les données associées de chaque chiffrement lient la valeur à son emplacement (`db.<colonne>`, fichier privé) :
  une valeur déplacée ou modifiée est refusée.
- Valeur de base : `jrv1:` + base64(nonce ‖ texte chiffré ‖ tag). Fichier : en-tête `JRV1` + nonce + texte chiffré.

## Fonctionnement

- Base : des déclencheurs SQLite temporaires (propres à chaque connexion de JobRadar) chiffrent les colonnes privées
  à l'écriture ; une fabrique de lignes les déchiffre à la lecture.
- Fichiers : toute écriture sous un dossier protégé est chiffrée ; les fichiers produits par le navigateur
  (captures, PDF) sont scellés à la fin de chaque candidature et à chaque actualisation quotidienne.
- Un document à déposer dans un formulaire est déchiffré dans un dossier temporaire en 0700, supprimé ensuite.
- Coffre fermé (trousseau verrouillé ou absent) : aucune écriture privée n'est possible.

## Activation, migration, récupération, rotation

1. Au premier lancement, l'écran du coffre crée la clé et affiche la clé de récupération une seule fois.
2. Après confirmation de sa sauvegarde, « Chiffrer mes données » chiffre tout l'existant. L'opération est
   reprenable ; elle termine par un `VACUUM` pour qu'aucune ancienne page en clair ne subsiste.
3. Sur un autre ordinateur ou après la perte du trousseau : `python -m jobradar.cli restaurer sauvegarde.zip` dans un
   dossier vide, puis la clé de récupération dans JobRadar réenveloppe la clé de données avec une nouvelle clé principale.
4. Rotation : `vault.rotate_master()` remplace la clé principale sans rechiffrer les données.

## Limites irréductibles

- Les cookies et sessions du navigateur du robot (`data/navigateur-systeme/`) sont gérés par le navigateur et ne
  peuvent pas être chiffrés par JobRadar.
- Les captchas et la double authentification restent à faire par toi.
- Les sauvegardes antérieures à l'activation du coffre sont en clair : Réglages → Coffre chiffré les signale et
  propose de les supprimer.
- Perdre à la fois le trousseau et la clé de récupération rend les données privées définitivement illisibles.
