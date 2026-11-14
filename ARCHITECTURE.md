# Architecture — Taxi Downtown Bot

Bot Discord (discord.py 2.7+, Python 3.13) pour l'entreprise de taxi Taxi Downtown sur Sensity RP. Un seul processus : client Discord + petit serveur HTTP (aiohttp) + base SQLite.

## Arborescence

```
.
├── bot.py                  Cœur : client, règles métier, commandes, panels, tâches de fond (~2100 l.)
├── features.py             Registre de configuration /config : catégories, modules, champs, GuildConfig (~420 l.)
├── config_ui.py            Écrans /config en Components V2 : accueil → catégorie → module (~360 l.)
├── storage.py              Accès SQLite (TaxiStore) : schéma, migrations, toutes les requêtes (~830 l.)
├── data/taxi_downtown.sqlite3   Base de données (WAL) — contient des données réelles : ne pas publier
├── tests/                  42 tests (unittest), toujours sur une base temporaire
│   ├── test_command_registry.py   liste exacte des commandes, commandes supprimées
│   ├── test_config.py             registre, états, valeurs, journaux, limites des écrans /config
│   ├── test_health.py             /health, profils, échappement des mentions
│   ├── test_rules.py              tarifs, primes, grades, panels PDS-FDS et tickets
│   └── test_storage.py            SQLite : services, pauses, flotte, avertissements, migrations
├── scripts/post-merge.sh   uv sync --frozen puis tests après un Pull Replit
├── pyproject.toml, uv.lock Dépendances (aiohttp, discord-py, typing-extensions)
├── .replit                 Lancement `python bot.py`, attend le port 8000
├── src/repl_nix_workspace/ Paquet vide exigé par le build backend uv
├── .agents/memory/         Notes de contexte pour l'agent Replit
├── README.md, ARCHITECTURE.md, LICENSE
```

## Couches et dépendances

```
Discord ──► bot.py ──► config_ui.py ──► features.py ──► storage.py ──► SQLite
Jeu (HTTP) ─► bot.py (aiohttp :8000)        ▲
                                   bot.py ───┘ (STORE, gcfg)
```

- `storage.py` ne connaît rien de Discord.
- `features.py` ne connaît que `storage.py` : il décrit la configuration et lit/écrit les valeurs.
- `config_ui.py` construit les écrans. Il n'importe pas `bot.py` : `bot.py` lui donne les actions (publier un panel) et le journal via `config_ui.setup(...)`, ce qui évite l'import circulaire (le bot est lancé comme `__main__`).
- `bot.py` assemble le tout.

## storage.py — base de données

| Table | Rôle |
|---|---|
| `guild_settings` | Réglages historiques : rôles Direction/Chauffeur, salons de journaux et webhook, catégories de tickets |
| `module_config` | Valeurs du `/config` (clé/valeur par serveur) : interrupteurs `mod.<module>` et champs |
| `employees` | Statut d'un employé : actif, inactif, refusé |
| `service_sessions`, `service_breaks` | Services et pauses (heures de la semaine, panel en direct) |
| `trips` | Courses et montants facturés |
| `vehicles` | Flotte (plaque, modèle, chauffeur attribué) |
| `warnings` | Avertissements RH |
| `schedules` | Plannings |
| `tickets` | Tickets ouverts et fermés (un par membre à la fois) |
| `payouts` | Primes versées (`/payerprime`) |
| `runtime_state` | Heartbeat, pointeur du panel PDS-FDS, drapeaux de migration |

Le schéma est créé et migré au démarrage (`initialize`) : les anciennes « plaques supplémentaires » sont fusionnées dans `vehicles` une seule fois. Chemin de base : variable `TAXI_DB_PATH`, sinon `data/taxi_downtown.sqlite3`.

## features.py — modèle de configuration

- 8 catégories → 28 modules → 38 champs (types : rôle, salon, catégorie, entier, décimal, texte).
- Chaque module a un interrupteur (sauf rôles et seuils), des champs, éventuellement des actions (publier un panel).
- Valeurs : colonnes de `guild_settings` pour les anciens réglages, `module_config` pour le reste ; défauts = règles Taxi Downtown, donc rien ne change tant qu'on ne modifie rien.
- État d'un module : `on` (actif et complet), `todo` (actif mais rôle/salon requis manquant), `off`.
- Catégories : Accès & Hiérarchie · Journaux · Service & Annonces · Tickets · Primes & Tarifs · Flotte & RH · Système.

## config_ui.py — `/config`

Message privé (administrateurs uniquement), expire après 15 minutes. Écrans `home_view` → `category_view` → `module_view`. Menus de rôles/salons, bouton Modifier (formulaire de 5 champs maximum), Activer/Désactiver, Réinitialiser, publication des panels. Chaque changement est consigné dans le journal général. Les écrans respectent les limites Discord (40 composants, 4000 caractères), testées.

## bot.py — organisation

1. **Constantes et règles** : `Rules` (quota, primes, pause max), `GuildConfig` via `gcfg(guild_id)`, `rules_for(guild_id)`.
2. **Garde-fous** : `require_guild`, `require_staff` (Direction ou administrateur), `require_feature(module)` (refuse si le module est désactivé).
3. **Client `TaxiBot`** : `setup_hook` (lie le port 8000 d'abord, récupère les services interrompus, enregistre les vues persistantes, synchronise les commandes), serveur HTTP, heartbeat.
4. **Tâches de fond** : `pause_watchdog` (rappel de pause), `auto_backup` (copie quotidienne de la base), heartbeat.
5. **Journaux** : `log_event` choisit le salon selon le type (service, tickets, RH), sinon le salon général ; respecte les interrupteurs.
6. **Règles métier** : classement des primes, tarif par zone, grades et promotions, profil employé.
7. **Commandes slash** et **panels à boutons** (voir ci-dessous).

### Commandes (19)

| Commande | Accès | Module qui la gouverne |
|---|---|---|
| `/info` (`membre:` = profil) | tous (profil d'un autre : Direction) | — |
| `/tarif` | tous | Tarifs |
| `/course` | chauffeurs en service | Courses |
| `/mystats` | tous (`membre:` Direction) | — |
| `/leaderboard` | tous | Primes |
| `/myvh`, `/checkvh` | tous (liste complète : Direction) | Flotte |
| `/horaire` | tous (modifier : Direction) | Planning |
| `/activite` | Direction | — |
| `/addvh`, `/delvh`, `/assignvh` | Direction | Flotte |
| `/recruter`, `/kickj` | Direction | Recrutement & départs |
| `/averto`, `/delaverto` | Direction | Avertissements |
| `/payerprime` | Direction | Primes |
| `/ouvert`, `/fermer` | Direction | Annonces ouvert/fermé |
| `/config` | administrateurs | — |

### Panels à boutons (persistants après redémarrage)

- **PDS-FDS** (`PdsView`, `td:pds:*`) : prise/fin de service, pause, reprise. Le message affiche en direct les employés en service (🟢) et en pause (🟡) ; il est actualisé à chaque action (`request_pds_refresh`, regroupé pour respecter les limites Discord).
- **Tickets** (`TicketPanelView`, `td:ticket:*`) : Candidature, Support, Signalement, Partenariat → formulaire (`TicketModal`) → salon privé (demandeur + Direction) dans la catégorie du type ; `TicketCloseView` ferme et supprime le salon.

### Serveur HTTP (port 8000)

- `GET /health` : état de Discord et de la base.
- `POST /webhook/ingame` : événements du jeu, actif seulement si `INGAME_WEBHOOK_SECRET` est défini et envoyé dans `X-Webhook-Secret`.

## Chemin d'une commande

`interaction` → `require_guild/staff` → `require_feature(module)` (réglages lus via `gcfg`) → règles du serveur (`rules_for`) → lecture/écriture `STORE` → réponse privée → `log_event` → actualisation du panel PDS-FDS si besoin.

## Déploiement

- Secrets : `DISCORD_TOKEN` (obligatoire), `INGAME_WEBHOOK_SECRET` (optionnel).
- Permissions Discord : commandes slash, Gérer les rôles (recrutement), Gérer les salons (tickets), écriture dans les salons de journaux ; le rôle du bot doit être au-dessus du rôle Chauffeur.
- Une seule instance à la fois (le port 8000 sert de verrou).
- Replit : bouton Run pour tester ; Deployment de type Reserved VM pour une disponibilité continue.
