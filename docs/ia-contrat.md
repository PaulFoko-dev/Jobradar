# Contrat avec l'assistant IA

## Ce que l'IA reçoit et ne reçoit jamais

| Reçoit | Ne reçoit jamais |
|---|---|
| Contenu public et assaini de la page (libellés, options, messages d'erreur) | Valeurs saisies, champs cachés, jetons, cookies |
| Identifiants abstraits des champs (`id` numérotés) | Nom, e-mail, téléphone, adresse, liens personnels |
| Catalogue des clés de profil disponibles (`email`, `country`…) **sans valeur** | Réponses types, salaire, disponibilité |
| Contenu professionnel du CV (expériences, projets, compétences), avec consentement | Identifiants de connexion, chemins locaux, secrets |

Les coordonnées présentes dans un CV sont relevées **localement** ; seul un texte masqué (`{{NOM}}`, `{{EMAIL}}`…) sert
à extraire la formation et la situation actuelle. Les tests `tests/test_prompt_leaks.py` vérifient chaque tâche IA
avec des valeurs sentinelles.

## Actions autorisées au superviseur du robot

`fill(id, source_key)`, `select(id, source_key)` ou `select(id, option)` pour un choix sans donnée personnelle,
`upload(id, cv|lettre)`, `click(id, purpose)`, `check(id)` (consentement à la candidature uniquement), `stop(reason)`.
Le programme résout la valeur en local (`application_supervisor.match_option`, `application_browser.best_option`) et
refuse toute action hors de l'instantané, sur un champ sensible ou vers une case de newsletter.

## Mémoire des réponses

| Concept | Validité |
|---|---|
| Disponibilité | 30 jours |
| Préavis, mobilité, salaire, durée de stage | 90 jours |
| Autorisation de travail, visa, convention de stage | 365 jours |
| Réponse apprise d'un formulaire sans concept reconnu | 180 jours |
| Identité, diplômes, permis | sans expiration |

Une réponse expirée n'est plus utilisée : le formulaire la redemande. Une nouvelle réponse crée une version ;
l'ancienne reste dans l'historique. Les questions sont rapprochées par concept, en français ou en anglais
(« When can you start? » → disponibilité), et « Oui » vaut « Yes ».

## Règles anti-invention des CV

- Le classement local (`cv_engine.rank`) ordonne expériences, projets, puces et compétences selon l'offre ; l'IA
  sélectionne et reformule à partir de ce classement.
- Toute puce reste rattachée à un élément du profil ; un nombre, un outil, une compétence, une date, un rôle ou un
  résultat absent de l'élément entraîne un refus.
- Une reformulation refusée a droit à **un** nouvel essai, motivé par la raison du refus ; ensuite le texte du profil
  est conservé.
- Chaque puce du CV est associée à sa preuve (texte source, termes de l'offre mis en avant, justification), visible
  dans l'onglet Documents.
- Les coordonnées sont injectées localement au rendu, après tous les appels IA.
- Une compétence sans preuve dans le profil peut figurer dans la liste des compétences, jamais dans une puce.
