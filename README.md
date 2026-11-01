# Taxi Downtown — bot Discord (Sensity RP)

Bot de gestion de l'entreprise de taxi Taxi Downtown : service, courses, primes hebdomadaires, flotte, planning et RH. Les commandes de gestion sont réservées aux administrateurs et au rôle Direction (sans rôle Direction configuré : administrateurs uniquement).

## Mise en route

1. Secret Replit `DISCORD_TOKEN` (jamais dans le code).
2. Permissions du bot : `applications.commands`, **Gérer les rôles** (recrutement), lecture/écriture dans le salon de journaux. Son rôle doit être au-dessus du rôle Chauffeur.
3. Un administrateur lance `/config` : un panel privé avec des menus (rôle Direction, rôle Chauffeur, salon des journaux, salon webhook) et deux boutons pour publier les panels dans le salon courant.
4. Panels publics (boutons persistants, valables après redémarrage) :
   - **PDS-FDS** : Prise de service, Fin de service, Pause, Reprise (remplace `/service`). Le panel affiche en direct les employés en service (🟢) et en pause (🟡) ; il se met à jour à chaque action. Seul le dernier panel publié est actualisé.
   - **Tickets** : Candidature, Question / Support, Signalement / Plainte. Chaque ticket crée un salon privé (demandeur + Direction) dans la catégorie du salon du panel ; bouton « Fermer le ticket » pour le demandeur ou la Direction. Le bot a besoin de **Gérer les salons**.
5. Données : `data/taxi_downtown.sqlite3`. Au premier démarrage, les anciennes « plaques supplémentaires » sont fusionnées dans la flotte (une seule fois).

## Règles appliquées

- Pas de salaire : **primes hebdomadaires** versées le dimanche soir — 1re place 20 000 $, 2e 15 000 $, 3e 10 000 $.
- **Minimum 10 h de service** dans la semaine (lundi–dimanche, heure de Paris) pour être éligible.
- Classement : argent rapporté, puis nombre de courses, puis heures de service.
- Pause : 30 minutes consécutives maximum (rappel privé + journal).
- Tarifs au km facturé (arrondi au supérieur à partir de x,8) : ville 700 $, hors ville 1 000 $, Cayo 1 300 $ ; attente 50 $/min, passager supplémentaire +50 $, gros bagage +25 $, réservation +50 $, transport médical 300–600 $.

## Éligibilités (`/info membre:`)

Top 1 heures, quota de 10 h (sinon avertissement ; déjà averti = licenciement / démission), prime estimée et passage de grade : Novice → Confirmé 7 500 $ + 7,5 h sans avertissement ; Confirmé → Senior 8 500 $ + 8,5 h sur 2 semaines ; Senior → Formateur / Chef de service 15 000 $ + 15 h + entretien et validation Direction. Le grade est lu dans le nom des rôles Discord (Novice, Confirmé, Senior, Formateur, Chef de service).

## Commandes

**Tous** : `/info` (règles, tarifs, passages de grade ; `membre:` = éligibilités, soi-même ou direction) · `/tarif` (calcul d'une course) · `/course` · `/mystats` · `/leaderboard` · `/myvh` · `/checkvh plaque` · `/horaire`

**Direction** : `/config` (admin : panel de configuration et publication des panels) · `/activite` (sous 10 h / inactifs 7 jours) · `/mystats membre:` · `/checkvh` sans plaque (flotte complète) · `/addvh` · `/delvh` · `/assignvh` · `/recruter` · `/averto` · `/delaverto` · `/kickj` · `/horaire planning:`

`/kickj` retire le rôle Chauffeur, passe l'employé en inactif, clôture son service ouvert et libère ses véhicules ; il n'expulse pas du serveur Discord. Après un redémarrage, les services encore ouverts sont arrêtés à la dernière activité connue du bot.

## Webhook in-game

`GET /health` (port 8000) indique l'état de Discord et de la base. `POST /webhook/ingame` n'est actif que si le secret `INGAME_WEBHOOK_SECRET` est défini et envoyé dans l'en-tête `X-Webhook-Secret` :

```json
{"guild_id": 123456789012345678, "event": "course_terminee", "details": {"chauffeur": "Nom RP", "montant": 1250}}
```

## Tests

`python -m unittest discover -s tests`
