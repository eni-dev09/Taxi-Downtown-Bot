# Architecture technique — Taxi Downtown

Ce document décrit l'organisation actuelle du dépôt. Il distingue les composants réellement présents des évolutions proposées.

## Vue d'ensemble

```text
Discord (slash commands, interactions, views)
                  |
                  v
               bot.py
      commandes, règles métier, vues,
      permissions, tâches et HTTP
          |                 |
          v                 v
      storage.py       aiohttp endpoints
      TaxiStore        GET /health
          |            POST /webhook/ingame
          v
  data/taxi_downtown.sqlite3
```

## Modules actuels

- **bot.py** : point d'entrée et application Discord. Regroupe les commandes, les interactions, les vues persistantes, les règles métier, les tâches périodiques, le heartbeat et les endpoints HTTP.
- **storage.py** : couche de persistance SQLite via `TaxiStore`. Elle gère les paramètres par serveur, employés, sessions, pauses, courses, véhicules, avertissements, plannings, tickets et paiements.
- **data/** : emplacement de la base SQLite locale. Les données d'exécution ne doivent pas être versionnées.
- **tests/** : tests unitaires de l'enregistrement des commandes, de l'état de santé, des règles métier et du stockage.
- **scripts/post-merge.sh** : synchronisation de l'environnement Python et lancement des tests après fusion.
- **.replit**, **pyproject.toml**, **uv.lock** : configuration d'exécution Replit et environnement Python reproductible.

## Flux de démarrage

1. Python exécute `bot.py`.
2. Le bot initialise le stockage et les extensions HTTP.
3. Les commandes et vues Discord sont enregistrées/restaurées.
4. Les tâches de surveillance et de maintenance démarrent.
5. Les sessions interrompues sont réconciliées avec l'état persistant.

## Données et sécurité

- Garder `DISCORD_TOKEN` et `INGAME_WEBHOOK_SECRET` dans les secrets de l'hébergeur ou dans un fichier local ignoré par Git.
- Ne jamais committer une base réelle, ses fichiers WAL/SHM, des sauvegardes ou des exports contenant des données de joueurs.
- Le webhook doit vérifier le secret avant de traiter un événement.
- Les opérations sensibles Discord doivent rester protégées par les permissions et contrôles de rôle.
- Effectuer des sauvegardes cohérentes de SQLite et tester régulièrement leur restauration.

## Commandes de vérification

```bash
uv sync --frozen
python -m unittest discover -s tests -v
```

## Pistes d'évolution (non appliquées)

Le fichier `bot.py` concentre encore beaucoup de responsabilités. Pour limiter les régressions, extraire progressivement les commandes, services métier, vues Discord et endpoints HTTP en modules séparés, en gardant `storage.py` comme couche d'accès aux données. Chaque extraction devrait être accompagnée de tests de non-régression.

Ne pas remplacer directement les fichiers principaux par une ancienne copie : comparer les comportements et migrations avant toute fusion.
