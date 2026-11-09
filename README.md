<div align="center">

<img src="docs/logo.png" alt="Logo JobRadar" width="128">

# JobRadar

**Ton radar personnel d'offres de stage, d'alternance et d'emploi, analysé par IA, entièrement sur ton PC.**

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-087CFA?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-stockage%20local-0B1F3A?logo=sqlite&logoColor=white)
![Plateformes](https://img.shields.io/badge/plateformes-Linux%20%7C%20macOS%20%7C%20Windows-555)
![IA](https://img.shields.io/badge/IA-Claude%20Code%20%7C%20Codex-08C5DC)
![Sans clé API](https://img.shields.io/badge/clé%20API-non%20requise-00D99B)
![100% local](https://img.shields.io/badge/données-100%25%20locales-087CFA)
[![Licence MIT](https://img.shields.io/badge/licence-MIT-0B1F3A)](LICENSE)

</div>

![Résultats](docs/screenshots/resultats.png)

## Pourquoi JobRadar ?

Chercher un stage, c'est ouvrir dix sites, retaper les mêmes mots-clés, retomber sur les mêmes offres
publiées partout et lire des dizaines d'annonces hors sujet. JobRadar fait ce travail pour toi :

1. **Tu décris ta recherche** avec un formulaire, une phrase (« stage bac+5 de 6 mois en cybersécurité en
   Île-de-France à partir de janvier »), ou les deux.
2. **L'IA en tire une stratégie de recherche** : requêtes FR/EN, intitulés ciblés, mots-clés du domaine,
   mots à exclure. Elle te signale aussi les contradictions. Tu valides ou tu ajustes.
3. **Des robots parcourent les sites d'offres depuis ton PC** : Indeed, LinkedIn, Welcome to the Jungle,
   HelloWork, France Travail et les pages carrières d'entreprises.
4. **Les offres sont filtrées, dédoublonnées, puis analysées par l'IA** : score de pertinence expliqué,
   résumé, missions, profil recherché et **mots-clés à reprendre dans ton CV**.
5. **Tu suis tes candidatures** dans un Kanban, de « Offre consultée » jusqu'à « Retenu ».

L'IA utilisée est **ton propre assistant en ligne de commande** (Claude Code ou Codex), déjà connecté à
ton abonnement. Pas de clé API, pas de serveur distant : tes recherches et tes candidatures restent sur ta machine.

## ✨ Fonctionnalités

| | |
|---|---|
| 🧠 **Analyse des critères par IA** | Formulaire et/ou langage naturel, puis requêtes, mots-clés et exclusions, modifiables avant lancement |
| 🌐 **Multi-sources** | Indeed · LinkedIn · Welcome to the Jungle · HelloWork · JobTeaser (connexion école) · La Bonne Alternance · Arbeitnow · Remote OK · France Travail · pages carrières (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) · catalogue de grands groupes (Thales, Airbus, Accenture, Alten, Sanofi…) |
| 🧹 **Filtrage et dédoublonnage** | Offres hors domaine ou d'un autre type de contrat écartées avant l'IA ; une même offre publiée sur plusieurs sites, ou menant au même formulaire, n'apparaît qu'une fois, avec tous ses liens ; une candidature équivalente à une candidature déjà envoyée est bloquée avant l'envoi |
| 📊 **Score de pertinence expliqué** | Points forts et points faibles, résumé, missions, profil, points d'attention |
| 📝 **Aide au CV** | Mots-clés exacts de l'offre à reprendre (copiables) et conseils d'adaptation |
| 👤 **Profil candidat** | Import du CV, réponses types datées qui expirent selon leur nature, versions réversibles, « Renforcer mon profil » (points faibles, compétences sans preuve), avatar métier |
| 📄 **CV et lettre adaptés** | Éléments du profil classés localement selon l'offre, puces reformulées sans invention et rattachées à leur preuve (« Pourquoi ce contenu »), contrôle ATS, DOCX d'origine conservé ou modèle JobRadar |
| 🚀 **Candidature contrôlée** | Choix des CV et lettres, amélioration d'une version par consigne libre avec nouvel aperçu, panneau manuel ou remplissage assisté sur Greenhouse, Lever, SmartRecruiters, Workday et formulaires génériques ; LinkedIn et Indeed restent manuels |
| 🤖 **Auto-candidature (optionnelle)** | Par recherche, depuis la page « Candidatures auto » : au-dessus d'un score choisi, CV et lettre préparés, contrôlés (ATS ≥ 95 %) puis envoyés automatiquement, avec un plafond quotidien ; Indeed et LinkedIn en préparation seule |
| 📍 **Zone stricte ou souple** | En mode souple, une offre hors zone reste visible si son score est élevé |
| 🗂️ **Suivi des candidatures** | Kanban par glisser-déposer, vue liste, notes, historique daté, taux de réponse et résultats par source, plateforme, tranche de score, CV de base, modèle ou mode d'envoi, mesures du robot de candidature |
| 📬 **Boîte de réception (optionnelle)** | Lue en local et sans rien marquer comme lu : réponses des recruteurs repérées et nouveau statut proposé, codes et liens de vérification des sites saisis par le robot ; rien n'est envoyé à l'IA |
| 🔁 **Confirmation automatique** | Quand tu reviens sur JobRadar après avoir ouvert une offre, il te demande si tu as postulé |
| ⏳ **Date limite et priorité** | Date limite de candidature extraite par l'IA (« Clôture aujourd'hui », « Bientôt close »), badge « Nouvelle », tri « À faire en priorité » |
| ⭐ **Règles par entreprise** | Entreprises favorites mises en avant ; entreprises exclues masquées et plus envoyées à l'IA ; alerte « Tu as déjà postulé chez X » |
| ↩️ **Relances guidées** | Rappel 10 jours après l'envoi, brouillon de relance rédigé par l'IA (objet et message modifiables, copie, ouverture dans ta messagerie), report ou désactivation |
| 📅 **Entretiens** | Planification dans la fiche de candidature, export vers ton agenda (`.ics`) avec rappel la veille, annulation exportable, fiche de préparation et simulation question par question |
| 🔔 **Notifications** | Offres notées 85 ou plus, relances à faire, entretiens à venir : dans JobRadar et sur le bureau |
| 🗄️ **Archive de chaque candidature** | Un dossier par candidature dans `candidatures/`, chiffré dès que le coffre est actif et consultable depuis JobRadar : copie PDF de la page de l'offre (texte complet vérifié), capture d'écran nette (résolution doublée), texte et analyse de l'offre, entretiens. Consultable même si l'annonce est retirée |
| 📦 **Sauvegarde et export** | Sauvegarde ZIP en un clic (base, réglages, profil, archives ; contenu privé chiffré), restauration sur un autre ordinateur avec ta clé de récupération, export du suivi en CSV pour Excel, badge « Annonce retirée » |
| ✉️ **Alertes par e-mail** | En option, un seul e-mail récapitulatif par actualisation sur ta propre adresse, envoyé depuis ton compte (TLS obligatoire, mot de passe dans le trousseau du système) |
| ⏰ **Mise à jour quotidienne** | systemd (Linux) ou Planificateur de tâches (Windows), rattrapée au démarrage si le PC était éteint |
| 🛟 **Résistant aux quotas** | Si ton quota IA est épuisé, les offres non analysées restent en attente et sont reprises le lendemain |
| 🔐 **Coffre chiffré et connexion locale** | Données privées chiffrées sur le disque (AES-256-GCM) avec clé de récupération ; accès à l'interface protégé par identifiant et mot de passe |
| 🌗 **Thème clair / sombre** | Interface responsive, utilisable aussi sur mobile, aux couleurs de JobRadar (Work Sans, bleu nuit, bleu radar, cyan, vert menthe) |

<table>
<tr>
<td width="50%"><img src="docs/screenshots/criteres.png" alt="Critères proposés par l'IA"></td>
<td width="50%"><img src="docs/screenshots/offre.png" alt="Détail d'une offre"></td>
</tr>
<tr>
<td align="center"><em>Critères générés par l'IA, modifiables avant lancement</em></td>
<td align="center"><em>Détail d'une offre : score, missions, aide au CV</em></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/suivi.png" alt="Suivi des candidatures"></td>
<td width="50%"><img src="docs/screenshots/sombre.png" alt="Écran Aujourd'hui en thème sombre"></td>
</tr>
<tr>
<td align="center"><em>Suivi des candidatures en Kanban</em></td>
<td align="center"><em>Écran « Aujourd'hui » en thème sombre</em></td>
</tr>
</table>

<p align="center"><img src="docs/screenshots/mobile.png" alt="Version mobile" width="260"><br><em>Version mobile</em></p>

## 🚀 Installation

### 1. Prérequis

- **Python 3.10 ou plus récent** : [python.org](https://www.python.org/downloads/). Sous Windows, coche
  « Add Python to PATH » pendant l'installation.
- **Un assistant IA en ligne de commande, connecté à ton compte** (un seul suffit) :

| Assistant | Abonnement | Installation | Vérification |
|---|---|---|---|
| **Claude Code** | Claude Pro / Max | [docs Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) | `claude -p "dis bonjour"` |
| **Codex CLI** | ChatGPT Plus / Pro | [github.com/openai/codex](https://github.com/openai/codex) | `codex exec "dis bonjour"` |

> Lance l'assistant une première fois dans un terminal pour te connecter. JobRadar l'utilisera ensuite
> tout seul, en arrière-plan.

### 2. Récupérer le projet et lancer

**Linux / macOS**

```bash
cd JobRadar
./run.sh
```

**Windows** : double-clique sur `run.bat`, ou dans un terminal :

```bat
cd JobRadar
run.bat
```

Le premier lancement crée l'environnement Python et installe les dépendances (1 à 2 minutes). Les
lancements suivants vérifient l'empreinte de `requirements.txt` et mettent automatiquement cet
environnement à jour si les dépendances ont changé. Ils exécutent également `pip check` et importent
les bibliothèques critiques : une installation incomplète est donc détectée même si l'empreinte n'a
pas changé. L'interface s'ouvre ensuite sur
**http://127.0.0.1:8765**.

La copie des pages d'offres utilise un navigateur Chromium piloté par JobRadar. S'il n'est pas déjà présent,
le premier lancement le télécharge (environ 150 Mo). Si ce téléchargement échoue, JobRadar démarre quand
même : les offres sont alors archivées en texte, sans copie de la page.

> 🧪 JobRadar a été testé sous **Linux**. Les scripts Windows (`run.bat`, `install_daily.ps1`) et la prise
> en charge de macOS sont fournis mais n'ont pas encore été éprouvés : n'hésite pas à remonter un problème.

### 3. Choisir l'assistant IA

JobRadar prend en charge **Claude Code et Codex CLI**. Le sélecteur « Analyse avec », en bas de la barre
latérale, permet de changer d'assistant sans modifier de fichier ni redémarrer l'application. Le choix est
enregistré dans `data/config.json` et s'applique aux prochaines analyses de critères, d'offres et de documents.

Une option est désactivée lorsque sa commande n'est pas installée. Lance l'assistant une première fois dans
un terminal pour terminer sa connexion. Les utilisateurs avancés peuvent toujours choisir le modèle par tâche
dans `"models"` ; une chaîne vide utilise le modèle par défaut de l'assistant sélectionné.

## 🧭 Utilisation

Au premier lancement, JobRadar propose de **créer ton compte local** (identifiant et mot de passe, qui protègent l'accès
à l'application sur cet ordinateur), puis lance une **visite guidée** qui te montre chaque partie de l'interface. Elle se
rouvre à tout moment avec le bouton **?** de la barre du haut ou depuis le menu de ton compte (avatar en haut à droite :
profil, réglages, connexion Google, guide, mot de passe, déconnexion). L'écran **Aujourd'hui** regroupe ensuite chaque
jour ce qui demande ton attention.

Commence par **Mon profil** (barre latérale) : importe ton CV et complète les informations souvent demandées. Dans
« Identité et présentation », **Choisir un avatar** ouvre une galerie d'avatars par métier (cybersécurité, data,
développement, infrastructure, produit) ; l'avatar choisi remplace tes initiales en haut à droite.

> 💡 Connecte un compte Google dans le navigateur de JobRadar (Réglages → Candidatures → « Connecter un compte
> Google ») : quand un site propose « Continuer avec Google », le robot s'en sert pour se connecter à ta place.

1. **+ Nouvelle recherche** : remplis le formulaire et/ou décris ta recherche, puis clique sur **Analyser**.
2. **Vérifie les critères proposés** : supprime ou ajoute des requêtes, des mots-clés ou des zones (touche Entrée pour ajouter).
3. **Enregistrer et lancer la recherche** : la collecte prend 15 à 30 minutes (les robots marquent des
   pauses pour ne pas surcharger les sites). Les cartes apparaissent au fil de l'analyse.
4. **Clique sur une carte**, puis sur **Candidater** : JobRadar ajoute l'offre au suivi et te laisse choisir
   les versions validées de ton CV et de ta lettre. Seuls les documents marqués « prêts à envoyer » sont proposés.
5. **En mode manuel**, utilise le panneau d'aide pour télécharger les documents choisis et copier tes coordonnées
   ou tes réponses types. Confirme l'envoi seulement après avoir réellement soumis le formulaire.
6. **Fais glisser tes candidatures** d'une colonne à l'autre au fil des réponses (entretien RH, technique, retenu, refusé…).
7. **Relance et prépare tes entretiens** : le bandeau « À relancer » du suivi propose un brouillon de relance ; la fiche
   d'une candidature permet d'ajouter un entretien et de l'exporter vers ton agenda.
8. **Garde un œil sur la cloche** 🔔 : elle signale les offres très pertinentes, les relances à faire et les entretiens du lendemain.
   Pour les recevoir aussi par e-mail, ouvre **Réglages** (engrenage, en bas de la barre latérale).

> 💡 Le filtre « Score ≥ 40 » masque par défaut les offres peu pertinentes et celles pas encore analysées.
> Mets-le à 0 pour tout voir.

### Préparation en arrière-plan et auto-candidature

La préparation du CV et de la lettre tourne sur le serveur : tu peux fermer la fenêtre, le résultat t'attend à la
réouverture de la candidature (onglet Documents). Une préparation interrompue par un arrêt de JobRadar repart au
démarrage suivant.

La page **Candidatures auto** (barre latérale) a un interrupteur par recherche (désactivé par défaut, confirmation
demandée). Il confie à JobRadar chaque offre notée au-dessus du **score minimal** choisi, dans la limite d'un
**plafond quotidien**. La page d'une recherche affiche l'état de son auto-candidature et un lien vers ce réglage :

1. l'offre est ajoutée au suivi, puis CV et lettre sont préparés avec tes bases et modèles par défaut, sans question ;
2. les documents sont générés ; la chaîne s'arrête si le contrôle ATS est sous 95 % ou si un document est périmé ;
3. la candidature part en **envoi assisté sans relecture** (ta préférence de relecture habituelle n'est pas modifiée).
   Pour Indeed, LinkedIn et les formulaires non pris en charge, JobRadar s'arrête aux documents prêts et te prévient.

La même page liste les candidatures automatiques récentes (recherche par texte, filtre par état) ; **Tout reprendre**
relance celles qui se sont arrêtées.

Les champs de motivation et les questions factuelles peuvent être complétés par l'IA uniquement à partir du profil ;
au moindre doute ou fait non justifié, JobRadar s'arrête avant l'envoi. Dans **Réglages → Candidatures**, le mode
**Navigateur du système** (par défaut) utilise ton navigateur par défaut s'il est basé sur Chromium (Chrome, Edge,
Brave, Vivaldi, Opera) ; avec Firefox ou Safari, qui ne peuvent pas être pilotés, il prend un Chrome ou un Edge
installé, sinon le Chromium intégré. Les navigateurs refusent d'être pilotés sur ton profil habituel : JobRadar ouvre
donc un profil à part, conservé d'une fois sur l'autre (`data/navigateur-systeme/`). Connecte-toi une première fois à
tes comptes avec le bouton **Ouvrir le navigateur pour me connecter** de cette section, ou :

```bash
python -m jobradar.cli navigateur
```

Des identifiants par domaine peuvent aussi être enregistrés dans cette section. Un identifiant n'est rempli que sur
le domaine enregistré ou l'un de ses sous-domaines : enregistré pour `acme.wd3.myworkdayjobs.com`, il n'est jamais
proposé à un autre client Workday. Les mots de passe restent dans le trousseau système (ou dans `data/secrets.json` protégé en 0600) et ne sont jamais affichés. Les captchas ne sont pas
contournés. La même section règle aussi :

- **l'attente d'une étape à faire toi-même** (connexion, création de compte, captcha, code reçu par e-mail) : 10 minutes
  par défaut, de 1 à 60. JobRadar reprend seul dès que c'est fait, sinon il met la candidature de côté et passe à la suivante ;
- **la création automatique des comptes** demandés par certains sites (Workday…), désactivée par défaut : ton e-mail, un
  mot de passe fort généré et rangé pour ce seul domaine, les conditions du compte acceptées (jamais une newsletter) ;
- **« Continuer avec Google »**, activé par défaut : le compte Google connecté dans le navigateur de JobRadar est utilisé
  quand un site le propose.

Si la lecture de la boîte de réception est activée (voir plus bas), un code ou un lien de vérification envoyé par le
site est saisi ou ouvert par le robot. Après un remplissage réussi, JobRadar retient pour chaque site quel champ
correspond à quelle information du profil (sans les valeurs) : à la visite suivante, ces champs sont remplis sans appel
à l'IA.

Toute difficulté (IA, validation, formulaire ambigu) arrête la chaîne et crée une notification. Une panne
du navigateur avant le clic d'envoi donne lieu à une unique reprise différée ; après le clic, aucune reprise automatique
n'est possible afin d'éviter un doublon. Une offre n'est jamais confiée deux fois. Avec la mise à jour
quotidienne sans serveur démarré, les documents sont préparés pendant `refresh` et l'envoi part au prochain
démarrage du serveur.

## ⏰ Mise à jour automatique quotidienne

Seules les recherches réglées sur **« Quotidienne »** sont actualisées. Le bouton **Actualiser maintenant**
fonctionne pour toutes.

**Linux** (timer systemd utilisateur ; une notification de bureau signale les nouvelles offres) :

```bash
./install_daily.sh 08:00
journalctl --user -u jobradar.service      # journal des exécutions
```

Le script accepte une heure au format strict `HH:MM`, vérifie l'installation locale, puis crée ou met à
jour de façon idempotente les unités dans `~/.config/systemd/user`. Il nécessite une distribution Linux
avec une session utilisateur systemd ; il ne convient pas à macOS. Quand `systemd-analyze` est disponible,
la syntaxe des deux unités est vérifiée avant installation. Si le rechargement ou l'activation échoue,
les fichiers et l'état précédents du timer sont restaurés au mieux.

> La validation complète du timer dépend de systemd : sur une installation minimale dépourvue de
> `systemd-analyze`, le script effectue les contrôles locaux puis laisse `systemctl --user daemon-reload`
> valider les unités. La restauration ne peut pas compenser une panne générale de la session systemd,
> mais les anciens fichiers d'unités sont remis en place avant de signaler l'erreur.

**Windows** (Planificateur de tâches, dans PowerShell) :

```powershell
powershell -ExecutionPolicy Bypass -File .\install_daily.ps1 -At 08:00
```

Le script vérifie l'environnement Python et crée ou remplace uniquement la tâche nommée `JobRadar`.
Il peut être relancé pour modifier son heure ou le chemin du projet.

Si le PC est éteint à l'heure prévue, l'exécution est rattrapée au prochain démarrage. Sur **macOS**,
lance `python -m jobradar.cli refresh` depuis une tâche `launchd` ou `cron`.

## ✉️ Alertes par e-mail

Désactivées par défaut. Une fois activées, chaque actualisation (bouton ou mise à jour quotidienne) envoie
**un seul e-mail récapitulatif** s'il y a du nouveau : offres très pertinentes (intitulé, entreprise, score,
verdict, trois raisons, lien), relances à faire et entretiens à venir. L'e-mail ne contient jamais ton CV,
tes lettres ni tes notes, et aucune image distante (pas de pixel de suivi).

1. Ouvre **Réglages** → **Alertes par e-mail**.
2. Indique ton **adresse de réception**, choisis ton **fournisseur** (Gmail, Outlook / Hotmail, Orange, Free ou
   Autre : le serveur, le port et la sécurité sont préremplis), puis ton **identifiant** et ton **mot de passe**.
3. Clique sur **Envoyer un e-mail de test** (les réglages sont enregistrés avant l'essai), puis active
   l'interrupteur et **Enregistre**.

| Fournisseur | Serveur | Port · sécurité | Remarque |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | 587 · STARTTLS | Exige un [mot de passe d'application](https://myaccount.google.com/apppasswords), disponible seulement avec la validation en deux étapes ; ton mot de passe Google habituel est refusé |
| Outlook / Hotmail | `smtp-mail.outlook.com` | 587 · STARTTLS | Les comptes personnels n'acceptent plus l'envoi par mot de passe, même d'application (Microsoft exige OAuth) : envoie depuis une autre adresse ; la réception sur Outlook reste possible |
| Orange | `smtp.orange.fr` | 465 · SSL | Adresse et mot de passe de ta messagerie Orange |
| Free | `smtp.free.fr` | 465 · SSL | Adresse et mot de passe de ta messagerie Free |

Ces préréglages sont indicatifs : en cas d'échec, compare-les avec l'aide de ton fournisseur. Le message
d'erreur du test (authentification refusée, serveur introuvable, délai dépassé, certificat invalide…) indique
la piste à suivre.

- **Chiffrement obligatoire** : STARTTLS (587) ou SSL (465) avec vérification du certificat. Si le serveur ne
  propose pas STARTTLS, rien n'est envoyé (jamais d'envoi en clair).
- **Mot de passe** : jamais écrit dans `config.json` ni renvoyé par l'interface. Il est lu dans l'ordre :
  variable d'environnement `JOBRADAR_SMTP_PASSWORD`, puis **trousseau du système** (Windows, macOS, Secret Service
  sous Linux), puis `data/secrets.json` (droits 0600), qui ne sert de repli que si le trousseau est
  indisponible ; l'interface l'affiche alors clairement.
- **Mise à jour quotidienne sous Linux** : le timer systemd peut ne pas accéder à un trousseau verrouillé
  (session fermée). L'erreur « Mot de passe SMTP introuvable » apparaît alors dans Réglages et dans
  `journalctl --user -u jobradar.service` ; définis `JOBRADAR_SMTP_PASSWORD` pour le service
  (`systemctl --user edit jobradar.service`, section `[Service]`, ligne `Environment=…`) si besoin.
- **Jamais bloquant** : un échec d'envoi est journalisé et l'alerte est reprise à l'exécution suivante, trois
  tentatives au plus. La recherche, elle, se termine normalement. Un e-mail de test au plus toutes les 10 s.
- Une alerte déjà lue ou ignorée dans JobRadar n'est pas envoyée par e-mail.

### Boîte de réception

Désactivée par défaut. Dans **Réglages → Alertes par e-mail**, section « Boîte de réception », coche « Lire ma boîte de
réception (en local) » : le choix est enregistré aussitôt. JobRadar utilise le même compte que les alertes par e-mail :
configure-le d'abord. Le serveur IMAP est déduit du serveur
d'envoi (`smtp.gmail.com` → `imap.gmail.com`) ; sinon, indique-le dans `inbox.imap_host` de `data/config.json`.

- La boîte est lue à chaque mise à jour quotidienne et avec le bouton **Vérifier ma boîte maintenant** : messages des
  30 derniers jours par défaut (200 au plus), en SSL et en lecture seule. Aucun message n'est marqué comme lu, déplacé
  ni supprimé.
- Chaque message est rapproché d'une candidature envoyée (nom ou domaine de l'entreprise), puis classé par règles :
  accusé de réception, refus ou entretien. Le statut n'est jamais changé seul : une notification te le **propose**.
  Seule exception : un accusé de réception confirme une candidature dont l'envoi n'avait pas été reconnu.
- Pendant une candidature, le robot y cherche le code ou le lien de vérification envoyé par le site.
- Le contenu des messages n'est jamais envoyé à l'IA.

## ⚙️ Configuration (`data/config.json`)

| Clé | Rôle | Défaut |
|---|---|---|
| `ai_provider` | `"claude"` ou `"codex"` | `"claude"` |
| `claude_bin` · `codex_bin` | Commande ou chemin complet de chaque assistant | `"claude"` · `"codex"` |
| `ai_timeout_s` | Délai maximal d'un appel à l'IA, en secondes | 600 |
| `models` | Modèle par assistant et par tâche (`criteria`, `scoring`, `follow_up`, `profile`, `tailoring`, `project`, `interview`) | sonnet / défaut Codex |
| `scoring_batch_size` · `scoring_parallel` | Offres par appel IA · appels simultanés | 4 · 3 |
| `archive_dir` | Dossier des archives de candidatures (vide = `candidatures/` dans le projet). Un dossier synchronisé (OneDrive, Nextcloud…) en fait une sauvegarde automatique | `""` |
| `timezone` | Fuseau horaire utilisé pour les dates limites, relances et entretiens | `"Europe/Paris"` |
| `follow_up_delay_days` | Jours après l'envoi avant de proposer une relance | 10 |
| `deadline_urgent_days` | Une date limite dans ce nombre de jours passe en tête du tri « À faire en priorité » | 7 |
| `alerts` | `min_score` : score à partir duquel une offre est signalée ; `desktop` : notifications de bureau | 85 · `true` |
| `application` | Préférences du parcours de candidature : `mode`, lettre incluse et validation avant envoi (`first_five`, `always` ou `never`) | manuel · lettre incluse · `first_five` |
| `email` | Alertes par e-mail (modifiables dans **Réglages**) : `enabled`, `to`, `from` (vide = identifiant), `smtp_host`, `smtp_port`, `security` (`"starttls"` ou `"ssl"`), `username`, `kinds` (types d'alerte envoyés). Jamais de mot de passe ici | désactivé · 587 · `"starttls"` · les 3 types |
| `inbox` | Lecture de la boîte de réception (modifiable dans **Réglages**) : `enabled`, `imap_host` (vide = déduit du serveur d'envoi), `days` (messages lus) | désactivé · `""` · 30 |
| `browser` | Navigateur des candidatures, réglable dans **Réglages → Candidatures** sauf `ai_supervision` : `engine` (`"system"` ou `"chromium"`), `executable`, `cdp_port`, `human_wait_minutes`, `auto_accounts`, `google_sign_in`, `ai_supervision` (`"always"`, `"fallback"` ou `"off"`) | navigateur du système · 9333 · 10 min · comptes manuels · Google activé · `"always"` |
| `max_offers_scored_per_run` | Plafond d'offres analysées par exécution (préserve ton quota) | 150 |
| `results_per_query` | Résultats demandés par requête et par site | 25 |
| `sources` | Active ou désactive chaque source | toutes sauf France Travail, La Bonne Alternance et JobTeaser |
| `francetravail` | `client_id` / `client_secret` gratuits sur [francetravail.io](https://francetravail.io) (API « Offres d'emploi v2 ») | vide |
| `labonnealternance` | `api_key` de lecture créée sur [l'espace développeurs officiel](https://api.apprentissage.beta.gouv.fr/) | vide |
| `companies` | Entreprises à surveiller directement : `{"name", "ats", "slug", "logo"}`, avec `ats` parmi `greenhouse`, `lever`, `ashby`, `smartrecruiters` et `workday` (Workday : `host` et `site` à la place de `slug`) | 5 exemples |

**Grands groupes** : la source `grands_groupes` interroge une quarantaine de groupes qui recrutent beaucoup en France
(Thales, Airbus, Accenture, Sanofi, Air Liquide, Valeo, Michelin, Stellantis, Alten, Assystem, Sopra Steria…), listés
dans `jobradar/sources/groups.py`. Sur Workday, JobRadar lance une recherche par requête de ta recherche, puis ne garde
que les offres situées dans tes zones. Les groupes sans API carrières ouverte (Capgemini, Orange, Société Générale…)
restent couverts par LinkedIn, Indeed, HelloWork et Welcome to the Jungle. Compte quelques minutes de collecte en plus.

**Ajouter une entreprise** : ouvre sa page carrières et regarde l'URL des offres.

| URL de la page carrières | `ats` | `slug` |
|---|---|---|
| `boards.greenhouse.io/datadog` | `greenhouse` | `datadog` |
| `jobs.lever.co/<entreprise>` | `lever` | `<entreprise>` |
| `jobs.ashbyhq.com/doctolib` | `ashby` | `doctolib` |
| `jobs.smartrecruiters.com/Devoteam` | `smartrecruiters` | `Devoteam` |
| `thales.wd3.myworkdayjobs.com/Careers` | `workday` | `"host": "thales.wd3.myworkdayjobs.com", "site": "Careers"` |

> 💸 **Quota** : une exécution complète représente quelques dizaines d'appels à l'IA. Avec Claude Code,
> `"haiku"` comme modèle de `scoring` est plus rapide et plus économe.

> 🧪 **Tester sans toucher à tes données** : la variable d'environnement `JOBRADAR_DATA_DIR` (chemin absolu)
> remplace le dossier `data/`, par exemple pour lancer JobRadar sur une copie de ta base.

## 🔧 Comment ça marche

```mermaid
flowchart LR
    A[Formulaire / texte libre] -->|IA| B[Critères structurés<br>requêtes · mots-clés · zones]
    B --> C[Collecte depuis ton PC<br>Indeed · LinkedIn · WTTJ · HelloWork · ...]
    C --> D[Filtrage<br>domaine · contrat · date · exclusions]
    D --> E[Dédoublonnage<br>entre sites et entre entreprises]
    E --> F[(SQLite local)]
    F -->|IA, par lots| G[Score · résumé · missions<br>mots-clés CV]
    G --> H[Interface web<br>cartes · filtres · Kanban]
```

- **Filtrage avant l'IA** : une offre n'est analysée que si son intitulé contient un mot-clé du domaine (ou
  sa description plusieurs) et, pour un stage ou une alternance, si le type de contrat correspond. Ça
  évite de dépenser ton quota sur des offres hors sujet.
- **Dédoublonnage** : les intitulés sont normalisés (accents, « H/F », « Ingénieur(e) »…) et comparés par
  entreprise. Un intitulé identique republié par un cabinet est aussi reconnu.
- **Reprise automatique** : une offre n'est marquée « analysée » qu'une fois l'analyse enregistrée. En cas
  de quota épuisé, de délai dépassé ou d'erreur, elle reste « en attente » et sera reprise à l'exécution
  suivante. Le bouton **analyser maintenant** permet aussi de relancer l'analyse sans refaire la collecte.
- **Sécurité** : l'assistant IA est lancé sans aucun outil (Claude Code `--tools ""`, Codex en bac à sable
  lecture seule). Il ne reçoit que du texte (offres, critères, formulaires assainis et, avec ton consentement, le
  contenu professionnel de ton CV) et répond en JSON ; les actions qu'il propose sont vérifiées localement
  ([docs/ia-contrat.md](docs/ia-contrat.md)).

## 🔒 Confidentialité et usage responsable

- Tout reste sur ton PC. Au premier lancement, JobRadar propose d'activer son **coffre chiffré** (AES-256) : profil,
  CV, lettres, archives des candidatures, captures du robot et données privées de la base sont alors chiffrés sur le
  disque. Une **clé de récupération**, affichée une seule fois, est le seul moyen de retrouver tes données sur un
  autre ordinateur (`python -m jobradar.cli restaurer sauvegarde.zip`, puis la clé dans JobRadar). Détails et limites :
  [docs/securite.md](docs/securite.md).
- L'assistant IA ne reçoit jamais ton identité, tes coordonnées, tes réponses types ni tes identifiants : seulement
  le texte des offres, tes critères et, avec ton consentement, le contenu professionnel de ton CV
  ([docs/ia-contrat.md](docs/ia-contrat.md)). Les messages de ta boîte de réception, si tu en actives la lecture,
  restent eux aussi sur ton PC.
- Indeed et LinkedIn interdisent la collecte automatisée dans leurs conditions d'utilisation. JobRadar
  consulte leurs pages publiques **sans connexion**, à faible volume et avec des pauses, pour un **usage
  personnel**. Un blocage temporaire reste possible ; les autres sources continuent alors de fonctionner.
  Ne l'utilise pas pour de la collecte massive.
- La Bonne Alternance est réservée aux usages non commerciaux. Arbeitnow et Remote OK imposent de conserver
  leur attribution et un lien vers l'annonce d'origine ; JobRadar les affiche comme source de chaque offre.
- L'API France Travail peut être temporairement restreinte. Son absence n'interrompt jamais les autres sources.
- **JobTeaser** (offres réservées aux étudiants) est lu dans le navigateur de JobRadar, après une connexion par ton
  école (Réglages → Candidatures → « Me connecter à JobTeaser », puis cocher la source). Si la session expire,
  JobRadar se reconnecte seul avec l'identifiant enregistré pour la page de l'école, sinon il saute JobTeaser pour ce
  scan et te prévient. Collecte à faible volume, uniquement quand JobRadar est ouvert, pour un usage personnel.
- L'APEC n'est pas prise en charge : ses offres sont derrière une protection anti-robot que JobRadar ne contourne pas.

## 🩺 Dépannage

| Problème | Solution |
|---|---|
| « Commande `claude` / `codex` introuvable » | Vérifie que la commande fonctionne dans un terminal ; sinon, indique son chemin complet dans `claude_bin` / `codex_bin` de `data/config.json` |
| Beaucoup d'offres « en attente d'analyse » | Quota atteint ou plafond par exécution : clique sur **analyser maintenant** plus tard, ou attends l'actualisation du lendemain |
| Une source renvoie 0 offre ou une erreur | Le site a pu changer ou te bloquer temporairement : consulte le **journal** pendant l'actualisation et désactive la source dans `sources` si besoin |
| Aucune carte affichée | Baisse le filtre de score à 0 et passe la zone en « Souple » |
| Le port 8765 est déjà utilisé | `run.sh --port 8780` (ou `run.bat --port 8780`) |
| Python est annoncé introuvable | Installe Python 3.10 ou plus récent. Sous Linux, installe aussi le paquet `python3-venv` si la création de `.venv` échoue |
| Les dépendances ne s'installent pas | Vérifie la connexion, puis relance `run.sh` ou `run.bat` : l'installation reprend sans toucher aux données utilisateur |
| La planification Linux échoue | Vérifie que `systemctl --user status` fonctionne ; certains conteneurs et environnements sans systemd ne prennent pas en charge le timer |

## 🗺️ Feuille de route

- [x] **Étape 1** : dates limites et priorité, règles par entreprise, alertes, relances guidées, entretiens dans l'agenda, qualité du dépôt
- [x] **Étape 2** : archivage local de chaque candidature (copie PDF et capture de l'offre), sauvegarde et export
- [x] **Étape 3** : profil candidat, CV et lettre adaptés à chaque offre, contrôle ATS
- [x] **Étape 4** : préparation et simulation d'entretien, contacts, compétences demandées et statistiques de candidature
- [x] **Étape 5** : candidature manuelle ou assistée par IA — Greenhouse, Lever, SmartRecruiters, Workday et formulaires génériques ; reprise humaine sûre, preuves et repli manuel
- [x] **Étape 6** : conservation du DOCX d’origine, reproduction ATS approchée du style d’un PDF Canva/Figma et modèle LaTeX ATS sécurisé avec repli automatique
- [ ] **Étape 7** : installateur, interface en anglais, clé API IA externe en option

## 📁 Structure du projet

```
JobRadar/
├── run.sh · run.bat              # lancement (Linux/macOS · Windows)
├── install_daily.sh · .ps1       # actualisation quotidienne (systemd · Planificateur de tâches)
├── requirements.txt
├── jobradar/
│   ├── server.py                 # API web (FastAPI)
│   ├── cli.py                    # serve · refresh · backup · navigateur · restaurer
│   ├── config.py                 # chemins et configuration (data/config.json)
│   ├── pipeline.py               # collecte → filtrage → dédoublonnage → analyse
│   ├── matching.py               # règles de pertinence et de dédoublonnage
│   ├── ai.py                     # appels à Claude Code / Codex, prompts et schémas JSON
│   ├── db.py · migrations.py     # stockage SQLite et évolutions versionnées du schéma
│   ├── workflow.py · today.py    # priorités, dates limites, relances · écran « Aujourd'hui »
│   ├── notifications.py          # alertes dans l'application, sur le bureau et récapitulatif e-mail
│   ├── mailer.py · inbox.py      # envoi SMTP chiffré · lecture locale de la boîte de réception (IMAP)
│   ├── auth.py                   # connexion locale (identifiant et mot de passe)
│   ├── vault.py · private_io.py  # coffre AES-256-GCM · fichiers privés chiffrés au repos
│   ├── profile.py                # profil candidat, import de sources, versions, avatar
│   ├── profile_coach.py · answer_memory.py  # « Renforcer mon profil » · réponses types datées
│   ├── cv_engine.py · tailoring.py  # classement local selon l'offre · CV et lettres adaptés
│   ├── render/                   # rendu PDF/DOCX/LaTeX et contrôle ATS
│   ├── preparation.py            # préparation en arrière-plan et auto-candidature
│   ├── application_*.py          # candidature : orchestration, file, formulaires, navigateur, supervision IA
│   ├── playbooks.py · login_credentials.py  # mémoire de parcours par site · identifiants par domaine
│   ├── interview_prep.py · interview_simulation.py · contacts.py · analytics.py
│   ├── archive.py · capture.py · redaction.py  # archives, copie des pages d'offres, assainissement
│   ├── export.py · calendar_export.py  # export CSV, sauvegarde ZIP · entretiens (.ics)
│   ├── sources/                  # un collecteur par site
│   └── static/                   # interface (HTML/CSS/JS, sans build), polices, avatars
├── tests/                        # tests automatisés (pytest)
├── docs/                         # sécurité, contrat IA, logo, captures
├── candidatures/                 # créé à l'usage : un dossier par candidature (ignoré par git)
└── data/                         # créé au lancement : base, configuration, secrets.json éventuel (ignoré par git)
```

## 📄 Licence

JobRadar est distribué sous [licence MIT](LICENSE) : tu peux l'utiliser, le modifier et le partager librement,
y compris pour un usage commercial, à condition de conserver la mention de copyright et de licence.
