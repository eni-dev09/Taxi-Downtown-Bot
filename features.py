"""Registre de configuration : catégories, modules, champs et valeurs par serveur.

Module pur (sans discord) : l'interface vit dans config_ui.py, le bot lit les
valeurs via GuildConfig. Chaque module peut être activé/désactivé et porte
des champs (rôle, salon, catégorie, nombre, texte) modifiables depuis /config.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from storage import DEFAULT_TICKET_CATEGORIES, TaxiStore

FieldKind = Literal["role", "channel", "category", "int", "float", "text"]
# Anciens réglages stockés dans les colonnes de guild_settings.
SETTINGS_STORE = "settings"
CONFIG_STORE = "config"

IMAGE_OUVERT = "https://i.postimg.cc/bN2rhrhh/image.png"
IMAGE_FERMER = "https://i.postimg.cc/qBNyNkT3/Taxi-Ferme.png"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: FieldKind
    default: Any = None
    store: str = CONFIG_STORE
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False
    help: str = ""


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    emoji: str
    description: str
    fields: tuple[Field, ...] = ()
    toggle: bool = True
    actions: tuple[tuple[str, str], ...] = ()  # (identifiant, libellé du bouton)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    emoji: str
    description: str
    modules: tuple[Module, ...]


def _role(key: str, label: str, **kw: Any) -> Field:
    return Field(key, label, "role", **kw)


def _int(key: str, label: str, default: int, lo: int, hi: int, **kw: Any) -> Field:
    return Field(key, label, "int", default, minimum=lo, maximum=hi, **kw)


def _ticket_module(kind: str, label: str, emoji: str, description: str) -> Module:
    return Module(
        f"ticket_{kind}",
        label,
        emoji,
        description,
        fields=(
            Field(
                f"ticket_category_{kind}_id",
                "Catégorie Discord des salons",
                "category",
                DEFAULT_TICKET_CATEGORIES[kind],
                store=SETTINGS_STORE,
            ),
        ),
    )


CATEGORIES: tuple[Category, ...] = (
    Category(
        "access",
        "Accès & Hiérarchie",
        "🛡️",
        "Direction, Chauffeur, grades, promotions",
        (
            Module(
                "direction", "Direction", "👔",
                "Rôle autorisé aux commandes de gestion (en plus des administrateurs).",
                fields=(_role("direction_role_id", "Rôle Direction", store=SETTINGS_STORE, required=True),),
                toggle=False,
            ),
            Module(
                "drivers", "Chauffeurs", "🚕",
                "Rôle attribué aux chauffeurs recrutés et requis pour la prise de service.",
                fields=(_role("driver_role_id", "Rôle Chauffeur", store=SETTINGS_STORE, required=True),),
                toggle=False,
            ),
            Module(
                "grades", "Rôles de grade", "🎖️",
                "Rôles Discord des grades, utilisés par /info (sinon détection par nom).",
                fields=(
                    _role("grade_role_novice", "Novice"),
                    _role("grade_role_confirme", "Chauffeur Confirmé"),
                    _role("grade_role_senior", "Chauffeur Senior"),
                    _role("grade_role_chef", "Formateur / Chef de service"),
                ),
                toggle=False,
            ),
            Module(
                "promo_novice", "Novice → Confirmé", "⬆️",
                "Conditions chiffrées du premier passage de grade.",
                fields=(
                    _int("promo_novice_money", "Argent rapporté ($)", 7500, 0, 10_000_000),
                    Field("promo_novice_hours", "Heures de service", "float", 7.5, minimum=0, maximum=500),
                ),
                toggle=False,
            ),
            Module(
                "promo_confirme", "Confirmé → Senior", "⬆️",
                "Conditions chiffrées (régularité sur 2 semaines).",
                fields=(
                    _int("promo_confirme_money", "Argent rapporté ($)", 8500, 0, 10_000_000),
                    Field("promo_confirme_hours", "Heures de service", "float", 8.5, minimum=0, maximum=500),
                ),
                toggle=False,
            ),
            Module(
                "promo_senior", "Senior → Chef de service", "⬆️",
                "Conditions chiffrées (entretien et validation Direction en plus).",
                fields=(
                    _int("promo_senior_money", "Argent rapporté ($)", 15000, 0, 10_000_000),
                    Field("promo_senior_hours", "Heures de service", "float", 15.0, minimum=0, maximum=500),
                ),
                toggle=False,
            ),
        ),
    ),
    Category(
        "logs",
        "Journaux",
        "📋",
        "Général, service, tickets, RH & flotte",
        (
            Module(
                "log_general", "Journal général", "🗒️",
                "Salon par défaut de tous les journaux du bot.",
                fields=(Field("log_channel_id", "Salon des journaux", "channel", store=SETTINGS_STORE, required=True),),
            ),
            Module(
                "log_service", "Journal PDS-FDS", "🕒",
                "Prises/fins de service, pauses (sinon salon général).",
                fields=(Field("log_channel_service", "Salon dédié", "channel"),),
            ),
            Module(
                "log_tickets", "Journal tickets", "🎫",
                "Ouvertures et fermetures de tickets (sinon salon général).",
                fields=(Field("log_channel_tickets", "Salon dédié", "channel"),),
            ),
            Module(
                "log_rh", "Journal RH & flotte", "📁",
                "Recrutements, avertissements, véhicules, plannings, primes (sinon salon général).",
                fields=(Field("log_channel_rh", "Salon dédié", "channel"),),
            ),
        ),
    ),
    Category(
        "service",
        "Service & Annonces",
        "🚖",
        "PDS-FDS, pauses, courses, ouvert/fermé",
        (
            Module(
                "pds", "Panel PDS-FDS", "🟢",
                "Boutons de prise/fin de service et pause, avec liste en direct.",
                actions=(("publish_pds", "Publier le panel ici"),),
            ),
            Module(
                "pauses", "Pauses", "⏸️",
                "Limite de pause consécutive et rappel automatique.",
                fields=(_int("max_break_minutes", "Pause maximale (minutes)", 30, 5, 180),),
            ),
            Module("course", "Courses", "🚕", "Commande /course pour enregistrer le montant facturé."),
            Module(
                "announce", "Annonces ouvert / fermé", "📢",
                "Commandes /ouvert et /fermer avec ping et image.",
                fields=(
                    _role("announce_role", "Rôle à mentionner"),
                    Field("announce_image_open", "Image « ouvert » (URL)", "text", IMAGE_OUVERT),
                    Field("announce_image_close", "Image « fermé » (URL)", "text", IMAGE_FERMER),
                ),
            ),
        ),
    ),
    Category(
        "tickets",
        "Tickets",
        "🎫",
        "Panel général : support, signalement, partenariat",
        (
            Module(
                "tickets", "Système de tickets", "🎫",
                "Active tous les tickets. Publie le panel avec ses boutons.",
                actions=(("publish_tickets", "Publier le panel ici"),),
            ),
            _ticket_module("support", "Question / Support", "❓", "Ticket de support."),
            _ticket_module("signalement", "Signalement / Plainte", "🚨", "Ticket de signalement."),
            _ticket_module("partenariat", "Partenariat", "🤝", "Ticket de partenariat."),
        ),
    ),
    Category(
        "recruitment",
        "Recrutement",
        "📋",
        "Panel de candidature séparé, formulaire et salon dédié",
        (
            Module(
                "ticket_recrutement", "Candidature Taxi Downtown", "📋",
                "Panel dédié avec bouton « Postuler maintenant » et formulaire de candidature.",
                fields=(
                    Field(
                        "ticket_category_recrutement_id",
                        "Catégorie Discord des candidatures",
                        "category",
                        DEFAULT_TICKET_CATEGORIES["recrutement"],
                        store=SETTINGS_STORE,
                    ),
                    Field("recruit_image", "Image du panel (URL, optionnel)", "text", ""),
                ),
                actions=(("publish_recruitment", "Publier le panel ici"),),
            ),
        ),
    ),
    Category(
        "money",
        "Primes & Tarifs",
        "💰",
        "Primes hebdo, quota, tarifs au km, suppléments",
        (
            Module(
                "primes", "Primes hebdomadaires", "🏆",
                "Quota d'heures, montants du podium, /leaderboard et /payerprime.",
                fields=(
                    Field("min_hours", "Quota d'heures par semaine", "float", 10.0, minimum=0, maximum=168),
                    _int("prime_1", "Prime 1re place ($)", 20000, 0, 10_000_000),
                    _int("prime_2", "Prime 2e place ($)", 15000, 0, 10_000_000),
                    _int("prime_3", "Prime 3e place ($)", 10000, 0, 10_000_000),
                ),
            ),
            Module(
                "tarifs", "Tarifs au km", "🧮",
                "Commande /tarif et grille affichée dans /info (km arrondi dès x,8).",
                fields=(
                    _int("rate_ville", "En ville ($/km)", 700, 1, 1_000_000),
                    _int("rate_hors_ville", "Hors ville ($/km)", 1000, 1, 1_000_000),
                    _int("rate_cayo", "Cayo ($/km)", 1300, 1, 1_000_000),
                ),
            ),
            Module(
                "supplements", "Suppléments", "➕",
                "Attente, passager, bagage, réservation (utilisés par /tarif).",
                fields=(
                    _int("fare_wait", "Attente ($/min)", 50, 0, 100_000),
                    _int("fare_passenger", "Passager supplémentaire ($)", 50, 0, 100_000),
                    _int("fare_luggage", "Gros bagage ($)", 25, 0, 100_000),
                    _int("fare_booking", "Réservation à l'avance ($)", 50, 0, 100_000),
                ),
                toggle=False,
            ),
        ),
    ),
    Category(
        "hr",
        "Flotte & RH",
        "🚗",
        "Véhicules, avertissements, planning, recrutement",
        (
            Module("fleet", "Flotte", "🚗", "Commandes /addvh, /delvh, /assignvh, /checkvh et /myvh."),
            Module("warnings", "Avertissements", "⚠️", "Commandes /averto et /delaverto."),
            Module("schedule", "Planning", "🗓️", "Commande /horaire."),
            Module(
                "recruit", "Recrutement & départs", "📋",
                "Commandes /recruter et /kickj. Rôles donnés à l'embauche : Chauffeur, Novice et entreprise.",
                fields=(_role("recruit_company_role", "Rôle entreprise (Taxi Downtown)"),),
            ),
        ),
    ),
    Category(
        "system",
        "Système",
        "⚙️",
        "Webhook in-game, sauvegardes",
        (
            Module(
                "ingame", "Webhook in-game", "🔗",
                "Reçoit les événements du serveur de jeu (secret INGAME_WEBHOOK_SECRET requis).",
                fields=(Field("webhook_channel_id", "Salon des événements", "channel", store=SETTINGS_STORE, required=True),),
            ),
            Module(
                "backup", "Sauvegarde automatique", "💾",
                "Copie quotidienne de la base de données.",
                fields=(_int("backup_keep", "Sauvegardes conservées", 5, 1, 30),),
            ),
        ),
    ),
)

MODULES: dict[str, Module] = {m.key: m for c in CATEGORIES for m in c.modules}
CATEGORY_OF: dict[str, Category] = {m.key: c for c in CATEGORIES for m in c.modules}
FIELDS: dict[str, Field] = {f.key: f for m in MODULES.values() for f in m.fields}
CATEGORY_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}


def parse_value(field: Field, raw: str) -> Any:
    """Validate user input for a number/text field. Raises ValueError (French message)."""
    text = raw.strip()
    if field.kind == "text":
        if not text:
            return None  # vide = valeur par défaut
        if not text.lower().startswith(("http://", "https://")):
            raise ValueError(f"{field.label} : une adresse http(s):// est attendue.")
        if len(text) > 400:
            raise ValueError(f"{field.label} : 400 caractères maximum.")
        return text
    cleaned = text.replace("$", "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        number = float(cleaned)
    except ValueError:
        raise ValueError(f"{field.label} : « {raw.strip()} » n'est pas un nombre.") from None
    if field.kind == "int":
        if number != int(number):
            raise ValueError(f"{field.label} : un nombre entier est attendu.")
        number = int(number)
    if field.minimum is not None and number < field.minimum:
        raise ValueError(f"{field.label} : minimum {field.minimum:g}.")
    if field.maximum is not None and number > field.maximum:
        raise ValueError(f"{field.label} : maximum {field.maximum:g}.")
    return number


class GuildConfig:
    """Snapshot of one guild's settings + module configuration."""

    def __init__(self, settings: dict[str, Any], values: dict[str, str]) -> None:
        self.settings = settings
        self.values = values

    @classmethod
    def load(cls, store: TaxiStore, guild_id: int) -> "GuildConfig":
        return cls(store.get_settings(guild_id), store.get_config(guild_id))

    # --- valeurs --------------------------------------------------------
    def get(self, key: str) -> Any:
        field = FIELDS[key]
        if field.store == SETTINGS_STORE:
            raw = self.settings.get(key)
            return int(raw) if raw not in (None, "") else None
        raw = self.values.get(key)
        if raw is None:
            return field.default
        if field.kind in ("role", "channel", "category"):
            return int(raw)
        if field.kind == "int":
            return int(float(raw))
        if field.kind == "float":
            return float(raw)
        return raw

    def is_set(self, key: str) -> bool:
        return self.get(key) not in (None, "")

    # --- modules --------------------------------------------------------
    def enabled(self, module_key: str) -> bool:
        module = MODULES[module_key]
        if not module.toggle:
            return True
        return self.values.get(f"mod.{module_key}", "1") != "0"

    def state(self, module: Module) -> str:
        """'on' (actif et prêt), 'todo' (actif mais incomplet) ou 'off'."""
        if module.toggle and not self.enabled(module.key):
            return "off"
        missing = [f for f in module.fields if f.required and not self.is_set(f.key)]
        if missing:
            return "todo"
        if not module.toggle and module.fields and all(f.default is None for f in module.fields):
            # Module sans valeur par défaut (ex. rôles de grade) : actif dès qu'un champ est rempli.
            return "on" if any(self.is_set(f.key) for f in module.fields) else "off"
        return "on"

    def category_counts(self, category: Category) -> tuple[int, int]:
        ready = sum(1 for m in category.modules if self.state(m) == "on")
        return ready, len(category.modules)

    # --- valeurs métier -------------------------------------------------
    def primes(self) -> tuple[int, int, int]:
        return (self.get("prime_1"), self.get("prime_2"), self.get("prime_3"))

    def zone_rates(self) -> dict[str, int]:
        return {
            "ville": self.get("rate_ville"),
            "hors_ville": self.get("rate_hors_ville"),
            "cayo": self.get("rate_cayo"),
        }

    def supplements(self) -> dict[str, int]:
        return {
            "wait": self.get("fare_wait"),
            "passenger": self.get("fare_passenger"),
            "luggage": self.get("fare_luggage"),
            "booking": self.get("fare_booking"),
        }

    def promotion(self, grade: str) -> tuple[int, float]:
        return self.get(f"promo_{grade}_money"), self.get(f"promo_{grade}_hours")


def set_field(store: TaxiStore, guild_id: int, field: Field, value: Any) -> None:
    """Persist one field (None = reset to default)."""
    if field.store == SETTINGS_STORE:
        if value is None:
            value = DEFAULT_TICKET_CATEGORIES.get(
                field.key.removeprefix("ticket_category_").removesuffix("_id")
            ) if field.kind == "category" else None
        store.set_settings(guild_id, **{field.key: value})
    else:
        store.set_config(guild_id, field.key, None if value is None else str(value))


def set_enabled(store: TaxiStore, guild_id: int, module_key: str, enabled: bool) -> None:
    store.set_config(guild_id, f"mod.{module_key}", "1" if enabled else "0")


def reset_module(store: TaxiStore, guild_id: int, module: Module) -> None:
    for field in module.fields:
        set_field(store, guild_id, field, None)
    if module.toggle:
        store.set_config(guild_id, f"mod.{module.key}", None)
