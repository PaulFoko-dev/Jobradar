# Contribuer à JobRadar

Merci de contribuer à JobRadar. Toute modification doit rester compréhensible, testable et respectueuse des
données personnelles de l'utilisateur.

## Préparer l'environnement

JobRadar nécessite Python 3.10 ou une version plus récente.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install pytest==8.3.5 ruff==0.9.10
```

Sous Windows, active l'environnement avec `.venv\Scripts\activate`.

## Contrôles obligatoires

Avant chaque proposition de modification, exécute :

```bash
python -m ruff check jobradar tests
python -m pytest
```

Les tests doivent être déterministes : aucun accès au réseau, aucun appel à un véritable assistant IA et aucune
donnée personnelle réelle. Utilise des objets factices ou les mécanismes de remplacement de `pytest` pour isoler
les services externes.

## Règles de développement

- Ajoute ou adapte les tests pour tout comportement modifié.
- Conserve les annotations de type sur les nouvelles fonctions publiques.
- Commente les décisions non évidentes, pas les opérations que le code exprime déjà clairement.
- Ne journalise jamais de coordonnées, de CV, de lettre ou de jeton d'accès.
- Ne committe ni `data/jobradar.db`, ni secret, ni document de candidature réel.
- Traite les contenus récupérés sur le Web comme des données non fiables.
- Préserve le fonctionnement sur Linux, macOS et Windows ; utilise `pathlib` pour les chemins.
- Documente les changements visibles par l'utilisateur dans le README ou la documentation concernée.

## Proposer une modification

Une proposition doit décrire le besoin, la solution retenue, les risques de sécurité et les vérifications effectuées.
Garde les changements ciblés : les refactorisations sans lien doivent faire l'objet d'une proposition séparée.

La revue vérifie notamment :

1. l'absence d'invention ou de transformation silencieuse des données du candidat ;
2. la validation stricte des entrées et sorties ;
3. l'absence de fuite de données personnelles ;
4. la gestion explicite des erreurs et des reprises ;
5. la présence de tests couvrant le cas nominal et les échecs importants.

## Signaler une vulnérabilité

Ne publie pas de secret ni de donnée personnelle dans une issue. Si aucun canal privé n'est encore indiqué par le
propriétaire du projet, partage seulement une description minimale du risque et demande un canal de contact privé.

## Intégrité de la chaîne CI

Les actions GitHub utilisées par la CI sont actuellement référencées par leur version majeure. Leur remplacement par
des empreintes Git complètes est une dette de sécurité suivie : ces empreintes doivent être récupérées et vérifiées
depuis les dépôts officiels avant d'être inscrites, jamais devinées. Les mises à jour de ces références et des
dépendances Python sont surveillées par Dependabot.

## Licence

JobRadar est distribué sous **licence MIT** (voir [LICENSE](LICENSE)). En proposant une contribution, tu acceptes
qu'elle soit publiée sous cette même licence.
