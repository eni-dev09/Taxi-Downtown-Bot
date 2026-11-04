"""Taxi Downtown Discord bot."""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import os
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks

from storage import TaxiStore, parse_iso, utc_now


PARIS = ZoneInfo("Europe/Paris")
# Primes hebdomadaires (versées le dimanche soir) : pas de salaire.
WEEKLY_PRIMES = (20_000, 15_000, 10_000)
MIN_HOURS_FOR_PRIME = 10.0  # quota hebdomadaire : en dessous = avertissement

# Passages de grade : (grade visé, argent rapporté, heures de service)
PROMOTIONS = {
    "novice": ("Chauffeur Confirmé", 7_500, 7.5),
    "confirme": ("Chauffeur Senior", 8_500, 8.5),
    "senior": ("Formateur & recruteur / Chef de service", 15_000, 15.0),
}
GRADE_LABELS = {
    "novice": "Novice",
    "confirme": "Chauffeur Confirmé",
    "senior": "Chauffeur Senior",
    "chef": "Formateur / Chef de service",
}
MAX_BREAK_SECONDS = 30 * 60

# Grille tarifaire Taxi Downtown.
ZONE_RATES = {"ville": 700, "hors_ville": 1000, "cayo": 1300}  # $ par km facturé
ZONE_LABELS = {"ville": "En ville", "hors_ville": "Hors ville", "cayo": "Cayo"}
FARE_WAIT_PER_MIN = 50
FARE_EXTRA_PASSENGER = 50
FARE_BIG_LUGGAGE = 25
FARE_BOOKING = 50
STORE = TaxiStore()

intents = discord.Intents.default()
intents.members = True


def week_bounds() -> tuple[datetime, datetime]:
    now = datetime.now(PARIS)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=now.weekday()
    )
    return start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(
        timezone.utc
    )


def format_duration(hours: float) -> str:
    minutes = max(0, round(hours * 60))
    return f"{minutes // 60} h {minutes % 60:02d}"


def format_money(amount: float) -> str:
    return f"{amount:,.0f} $".replace(",", " ")


def format_local_time(value: str | datetime) -> str:
    moment = parse_iso(value) if isinstance(value, str) else value
    return moment.astimezone(PARIS).strftime("%d/%m/%Y à %H:%M")


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(
        interaction.guild
        and isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def is_staff(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if not guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_id = STORE.get_settings(guild.id).get("direction_role_id")
    return bool(role_id and any(role.id == role_id for role in interaction.user.roles))


async def require_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is not None:
        return True
    await interaction.response.send_message(
        "Cette commande doit être utilisée sur le serveur Taxi Downtown.",
        ephemeral=True,
    )
    return False


async def require_staff(interaction: discord.Interaction) -> bool:
    if not await require_guild(interaction):
        return False
    if is_staff(interaction):
        return True
    await interaction.response.send_message(
        "Commande réservée à la direction. Configure le rôle Direction avec `/config`.",
        ephemeral=True,
    )
    return False


async def deny_if_blacklisted(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if not guild:
        return False
    # La fonction blacklist_entry n'est pas dans le storage fourni, mais on garde la logique
    # if STORE.blacklist_entry(guild.id, interaction.user.id):
    #     await interaction.response.send_message(
    #         "Tes interactions avec le service Taxi Downtown sont suspendues. Contacte la direction.",
    #         ephemeral=True,
    #     )
    #     return True
    return False


async def log_event(
    guild: discord.Guild, title: str, description: str, *, webhook: bool = False
) -> bool:
    settings = STORE.get_settings(guild.id)
    channel_id = settings.get("webhook_channel_id") if webhook else settings.get("log_channel_id")
    channel_id = channel_id or settings.get("log_channel_id")
    if not channel_id:
        return False
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await guild.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
    if not hasattr(channel, "send"):
        return False
    embed = discord.Embed(
        title=title[:256],
        description=description[:4000],
        color=discord.Color.gold(),
        timestamp=utc_now(),
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        print(f"Impossible d'envoyer un journal dans le salon {channel_id}.")
        return False


def weekly_totals(guild_id: int) -> tuple[datetime, datetime, dict[int, dict[str, float]]]:
    start, end = week_bounds()
    return start, end, STORE.weekly_stats(guild_id, start, min(end, utc_now()))


EMPTY_STATS = {"hours": 0.0, "fare": 0.0, "trips": 0.0}


def prime_ranking(
    stats: dict[int, dict[str, float]],
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (eligible, not_eligible) as (user_id, hours) lists.

    Eligible drivers (>= 10 h) are ranked by money brought, then number of
    trips, then hours. The first three receive the weekly primes.
    """
    eligible = [
        uid
        for uid, item in stats.items()
        if item["hours"] >= MIN_HOURS_FOR_PRIME
    ]
    eligible.sort(
        key=lambda uid: (stats[uid]["fare"], stats[uid]["trips"], stats[uid]["hours"]),
        reverse=True,
    )
    ranked = set(eligible)
    others = sorted(
        (uid for uid, item in stats.items() if uid not in ranked and item["hours"] > 0),
        key=lambda uid: stats[uid]["hours"],
        reverse=True,
    )
    return (
        [(uid, stats[uid]["hours"]) for uid in eligible],
        [(uid, stats[uid]["hours"]) for uid in others],
    )


def ordinal(n: int) -> str:
    return f"{n}re" if n == 1 else f"{n}e"


def prime_for(stats: dict[int, dict[str, float]], user_id: int) -> int:
    eligible, _ = prime_ranking(stats)
    for index, (uid, _hours) in enumerate(eligible[: len(WEEKLY_PRIMES)]):
        if uid == user_id:
            return WEEKLY_PRIMES[index]
    return 0


def billed_km(km: float) -> int:
    """Kilomètres facturés : arrondi à partir de x,8 (1,7 -> 1 ; 1,8 -> 2), minimum 1."""
    return max(1, int(km + 0.2 + 1e-9))


def compute_fare(
    km: float,
    zone: str = "ville",
    wait_minutes: int = 0,
    extra_passengers: int = 0,
    big_luggage: int = 0,
    booking: bool = False,
) -> tuple[int, list[str]]:
    """Tarif par zone : tarif/km × km facturé + suppléments."""
    units = billed_km(km)
    rate = ZONE_RATES[zone]
    total = units * rate
    lines = [
        f"{ZONE_LABELS[zone]} — {km:g} km → {units} km facturé(s) × {rate} : {format_money(total)}"
    ]
    for count, unit, label in (
        (wait_minutes, FARE_WAIT_PER_MIN, "Attente (min)"),
        (extra_passengers, FARE_EXTRA_PASSENGER, "Passager(s) supplémentaire(s)"),
        (big_luggage, FARE_BIG_LUGGAGE, "Gros bagage(s)"),
    ):
        if count:
            amount = count * unit
            total += amount
            lines.append(f"{label} {count} × {unit} : {format_money(amount)}")
    if booking:
        total += FARE_BOOKING
        lines.append(f"Réservation à l'avance : {format_money(FARE_BOOKING)}")
    return total, lines


def employee_ids(guild: discord.Guild, stats: dict[int, dict[str, float]]) -> set[int]:
    result = set(STORE.get_active_employee_ids(guild.id)) | set(stats)
    driver_role_id = STORE.get_settings(guild.id).get("driver_role_id")
    role = guild.get_role(int(driver_role_id)) if driver_role_id else None
    if role:
        result.update(member.id for member in role.members)
    return result


def member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"ID {user_id}"


def chunks(text: str, max_length: int = 1850) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines() or [text]:
        while len(line) > max_length:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_length])
            line = line[max_length:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_length:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or ["Aucune donnée à afficher."]


async def send_private_text(interaction: discord.Interaction, text: str) -> None:
    pieces = chunks(text)
    for index, piece in enumerate(pieces):
        if index == 0 and not interaction.response.is_done():
            await interaction.response.send_message(
                piece,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                piece,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class TaxiBot(commands.Bot):
    webhook_runner: web.AppRunner | None = None

    def __init__(self) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.interrupted_services: list[dict[str, Any]] = []
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.start_webhook_server()
        self.interrupted_services = STORE.recover_interrupted_services()
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        for view in (PdsView(), TicketPanelView(), TicketCloseView()):
            self.add_view(view)
        synced = await self.tree.sync()
        print(f"{len(synced)} commandes slash synchronisées.")

    async def heartbeat_loop(self) -> None:
        while not self.is_closed():
            try:
                STORE.set_runtime_heartbeat()
            except sqlite3.Error as exc:
                print(f"Heartbeat SQLite indisponible : {type(exc).__name__}.")
            await asyncio.sleep(15)

    async def start_webhook_server(self) -> None:
        shared_secret = os.getenv("INGAME_WEBHOOK_SECRET")
        app = web.Application(client_max_size=16 * 1024)
        app.router.add_get("/health", self.health_check)
        app.router.add_post("/webhook/ingame", self.receive_game_event)
        self.webhook_runner = web.AppRunner(app)
        await self.webhook_runner.setup()
        port = int(os.getenv("WEBHOOK_PORT", "8000"))
        site = web.TCPSite(self.webhook_runner, "0.0.0.0", port)
        try:
            await site.start()
        except OSError as exc:
            await self.webhook_runner.cleanup()
            self.webhook_runner = None
            if exc.errno == errno.EADDRINUSE:
                raise SystemExit(
                    f"❌ Le port {port} est déjà utilisé : une autre instance du bot tourne "
                    "(bouton Run, Shell ou Deployment). Arrête-la avant de relancer, sinon "
                    "le bot répondrait en double."
                ) from None
            raise
        if shared_secret:
            print(f"Webhook in-game prêt sur le port {port}, chemin /webhook/ingame.")
        else:
            print(
                f"Serveur de santé prêt sur le port {port}; webhook in-game désactivé "
                "car INGAME_WEBHOOK_SECRET n'est pas configuré."
            )

    async def health_check(self, request: web.Request) -> web.Response:
        del request
        database_ready = False
        try:
            with STORE._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            database_ready = True
        except sqlite3.Error as exc:
            print(f"Contrôle de santé SQLite en échec : {type(exc).__name__}.")

        discord_ready = self.is_ready()
        healthy = database_ready and discord_ready
        return web.json_response(
            {
                "status": "ok" if healthy else "degraded",
                "discord": "ready" if discord_ready else "not_ready",
                "database": "ok" if database_ready else "error",
                "webhook": "enabled" if os.getenv("INGAME_WEBHOOK_SECRET") else "disabled",
            },
            status=200 if healthy else 503,
            headers={"Cache-Control": "no-store"},
        )

    async def receive_game_event(self, request: web.Request) -> web.Response:
        shared_secret = os.getenv("INGAME_WEBHOOK_SECRET", "")
        supplied_secret = request.headers.get("X-Webhook-Secret", "")
        if not shared_secret or not hmac.compare_digest(shared_secret, supplied_secret):
            raise web.HTTPUnauthorized(text="Unauthorized")
        try:
            payload = json.loads(await request.text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Expected a JSON object") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        try:
            guild_id = int(payload["guild_id"])
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="guild_id is required") from None

        guild = self.get_guild(guild_id)
        if guild is None:
            raise web.HTTPNotFound(text="Unknown guild")
        event_name = str(payload.get("event", "Événement in-game")).strip()[:256]
        detail_value = payload.get("details", {})
        details = (
            detail_value
            if isinstance(detail_value, str)
            else json.dumps(detail_value, ensure_ascii=False, indent=2)
        )
        delivered = await log_event(
            guild,
            f"🎮 {event_name or 'Événement in-game'}",
            f"```json\n{details[:3800]}\n```",
            webhook=True,
        )
        if not delivered:
            raise web.HTTPServiceUnavailable(text="No webhook log channel configured or available")
        return web.json_response({"ok": True})

    async def close(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            try:
                await self.heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"Heartbeat arrêté de façon inattendue : {type(exc).__name__}.")
        try:
            STORE.set_runtime_heartbeat()
        except sqlite3.Error as exc:
            print(f"Impossible d'enregistrer le heartbeat de fermeture : {type(exc).__name__}.")
        if pause_watchdog.is_running():
            pause_watchdog.cancel()
        if auto_backup.is_running():
            auto_backup.cancel()
        if self.webhook_runner:
            await self.webhook_runner.cleanup()
        await super().close()


bot = TaxiBot()


@bot.event
async def on_ready() -> None:
    print(f"🚖 Taxi Downtown en ligne — connecté en tant que {bot.user}.")
    if not pause_watchdog.is_running():
        pause_watchdog.start()
    if not auto_backup.is_running():
        auto_backup.start()
    for session in bot.interrupted_services:
        guild = bot.get_guild(int(session["guild_id"]))
        if not guild:
            continue
        break_note = (
            " La pause active dépassait la limite de 30 minutes."
            if session["open_break_seconds"] > MAX_BREAK_SECONDS
            else ""
        )
        await log_event(
            guild,
            "⚠️ Service interrompu au redémarrage",
            f"Le service de <@{session['user_id']}> s'est arrêté autour de "
            f"{format_local_time(session['ended_at'])} après un redémarrage du bot."
            f"{break_note} Reprends ton service avec le panel PDS-FDS si nécessaire.",
        )
    for guild in bot.guilds:
        request_pds_refresh(guild)
    bot.interrupted_services.clear()


@tasks.loop(seconds=60)
async def pause_watchdog() -> None:
    try:
        overdue = STORE.overdue_breaks(utc_now() - timedelta(seconds=MAX_BREAK_SECONDS))
    except sqlite3.Error as exc:
        print(f"Surveillance des pauses indisponible : {type(exc).__name__}.")
        return
    for entry in overdue:
        try:
            marked = STORE.mark_break_reminded(int(entry["break_id"]))
        except sqlite3.Error as exc:
            print(f"Rappel de pause non enregistré : {type(exc).__name__}.")
            continue
        if not marked:
            continue
        guild = bot.get_guild(int(entry["guild_id"]))
        if not guild:
            continue
        member = guild.get_member(int(entry["user_id"]))
        if member:
            try:
                await member.send(
                    "⏰ Ta pause Taxi Downtown dépasse 30 minutes. "
                    "Termine-la avec le bouton Reprise du panel PDS-FDS et contacte la direction en cas d'exception."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        await log_event(
            guild,
            "⏰ Pause de plus de 30 minutes",
            f"<@{entry['user_id']}> a dépassé la limite de pause consécutive.",
        )


@pause_watchdog.before_loop
async def before_pause_watchdog() -> None:
    await bot.wait_until_ready()


@tasks.loop(hours=24)
async def auto_backup() -> None:
    """Sauvegarde la base de données toutes les 24h."""
    db_path = STORE.path
    if not db_path.exists():
        return
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    
    # Garder seulement les 5 dernières sauvegardes
    backups = sorted(backup_dir.glob("*.sqlite3"), key=os.path.getmtime)
    if len(backups) >= 5:
        backups[0].unlink()

    date_str = utc_now().strftime("%Y-%m-%d_%H-%M")
    dest = backup_dir / f"taxi_downtown_{date_str}.sqlite3"
    try:
        shutil.copy2(db_path, dest)
        print(f"✅ Sauvegarde de la base de données effectuée : {dest.name}")
    except Exception as e:
        print(f"❌ Échec de la sauvegarde : {type(e).__name__}")

@auto_backup.before_loop
async def before_auto_backup() -> None:
    await bot.wait_until_ready()


# --------------------------------------------------------------------------- #
# Informations
# --------------------------------------------------------------------------- #

def profile_text(value: str) -> str:
    safe = discord.utils.escape_mentions(
        discord.utils.escape_markdown(str(value), as_needed=False)
    )
    return safe.replace("<@", "<@​").replace("<#", "<#​")


def prime_status_line(stats: dict[int, dict[str, float]], user_id: int) -> str:
    hours = stats.get(user_id, EMPTY_STATS)["hours"]
    if hours < MIN_HOURS_FOR_PRIME:
        return (
            f"Prime : non éligible — il manque "
            f"{format_duration(MIN_HOURS_FOR_PRIME - hours)} (minimum {MIN_HOURS_FOR_PRIME:g} h)"
        )
    eligible, _ = prime_ranking(stats)
    rank = next(i for i, (uid, _h) in enumerate(eligible, start=1) if uid == user_id)
    prime = prime_for(stats, user_id)
    if prime:
        return f"Prime : **{ordinal(rank)} place — {format_money(prime)}**"
    return f"Prime : {ordinal(rank)} place (hors podium)"


def weekly_lines(stats: dict[int, dict[str, float]], user_id: int) -> list[str]:
    data = stats.get(user_id, EMPTY_STATS)
    return [
        f"Heures : {format_duration(data['hours'])} · Courses : {int(data['trips'])}",
        f"Argent rapporté : {format_money(data['fare'])}",
        prime_status_line(stats, user_id),
    ]


def member_grade(member: discord.Member) -> str | None:
    """Grade déduit du nom des rôles Discord (plus haut grade en priorité)."""
    names = " | ".join(
        unicodedata.normalize("NFKD", role.name).encode("ascii", "ignore").decode().lower()
        for role in getattr(member, "roles", [])
    )
    for key, needles in (
        ("chef", ("chef de service", "formateur")),
        ("senior", ("senior",)),
        ("confirme", ("confirme",)),
        ("novice", ("novice",)),
    ):
        if any(needle in names for needle in needles):
            return key
    return None


def normalized_role_name(value: str) -> str:
    """Compare role names without being sensitive to accents or case."""
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode()
        .casefold()
        .strip()
    )


def recruitment_roles(guild: discord.Guild, settings: dict[str, Any]) -> list[discord.Role]:
    """Return the Discord roles granted to a newly accepted chauffeur.

    The configured driver role is retained for servers whose role naming differs.
    The three standard Taxi Downtown roles are also found by name so a new hire
    always starts with the company, chauffeur and novice roles.
    """
    role_ids: set[int] = set()
    driver_role_id = settings.get("driver_role_id")
    if driver_role_id:
        role_ids.add(int(driver_role_id))
    wanted_names = {"taxi downtown", "chauffeur", "novice"}
    role_ids.update(
        role.id
        for role in guild.roles
        if normalized_role_name(role.name) in wanted_names
    )
    return [role for role in guild.roles if role.id in role_ids and not role.is_default()]


def previous_week_stats(guild_id: int) -> dict[int, dict[str, float]]:
    start, _end = week_bounds()
    return STORE.weekly_stats(guild_id, start - timedelta(days=7), start)


def meets(data: dict[str, float], need_money: int, need_hours: float) -> bool:
    return data["fare"] >= need_money and data["hours"] >= need_hours


def missing(data: dict[str, float], need_money: int, need_hours: float) -> str:
    parts = []
    if data["fare"] < need_money:
        parts.append(f"{format_money(need_money - data['fare'])} à rapporter")
    if data["hours"] < need_hours:
        parts.append(f"{format_duration(need_hours - data['hours'])} de service")
    return " + ".join(parts)


def employee_profile(guild: discord.Guild, member: discord.Member) -> str:
    start, _end, stats = weekly_totals(guild.id)
    data = stats.get(member.id, EMPTY_STATS)
    grade = member_grade(member)
    status = STORE.employee_status(guild.id, member.id)
    status_label = {
        "active": "Actif",
        "inactive": "Inactif",
        "rejected": "Candidature refusée",
    }.get(status or "", "Statut à vérifier")
    service = STORE.service_state(guild.id, member.id)
    state = "Hors service"
    if service:
        state = "En pause" if service["pause"] else "En service"
    warnings = STORE.warnings_for_user(guild.id, member.id)

    lines = [
        f"**Profil — {profile_text(member.display_name)}**",
        f"Grade : {GRADE_LABELS.get(grade, 'non déterminé')} · {status_label} · {state}",
        f"**Semaine depuis le {format_local_time(start)}**",
        f"Heures : {format_duration(data['hours'])} · Argent rapporté : "
        f"{format_money(data['fare'])} · Courses : {int(data['trips'])}",
        "**Primes**",
    ]

    by_hours = sorted(
        (uid for uid, item in stats.items() if item["hours"] > 0),
        key=lambda uid: stats[uid]["hours"],
        reverse=True,
    )
    if member.id in by_hours:
        hours_rank = by_hours.index(member.id) + 1
        lines.append(
            "🥇 Top 1 heures : **oui**"
            if hours_rank == 1
            else f"Top 1 heures : non ({ordinal(hours_rank)} en heures)"
        )
    else:
        lines.append("Top 1 heures : non (aucune heure)")
    quota_ok = data["hours"] >= MIN_HOURS_FOR_PRIME
    lines.append(
        f"Quota {MIN_HOURS_FOR_PRIME:g} h : ✅ atteint"
        if quota_ok
        else f"Quota {MIN_HOURS_FOR_PRIME:g} h : ❌ il manque "
        f"{format_duration(MIN_HOURS_FOR_PRIME - data['hours'])}"
    )
    lines.append(prime_status_line(stats, member.id))

    lines.append("**Avertissement**")
    if quota_ok:
        lines.append(
            "✅ Aucun avertissement à prévoir"
            + (f" (avertissements actifs : {len(warnings)})" if warnings else "")
        )
    elif warnings:
        lines.append(
            f"⛔ Quota non atteint avec {len(warnings)} avertissement(s) actif(s) : "
            "**licenciement / démission**"
        )
    else:
        lines.append("⚠️ Quota non atteint : **avertissement à prévoir**")
    for warning in warnings[:5]:
        lines.append(
            f"• #{warning['id']} — {format_local_time(warning['created_at'])} — "
            f"{profile_text(warning['reason'])}"
        )

    lines.append("**Passage de grade**")
    if grade in PROMOTIONS:
        target, need_money, need_hours = PROMOTIONS[grade]
        lines.append(f"Objectif : {target} ({format_money(need_money)} + {need_hours:g} h)")
        ok_now = meets(data, need_money, need_hours)
        lines.append(
            "Cette semaine : ✅ conditions remplies"
            if ok_now
            else f"Cette semaine : ❌ il manque {missing(data, need_money, need_hours)}"
        )
        if grade == "novice":
            lines.append(
                "Avertissement : ✅ aucun"
                if not warnings
                else "Avertissement : ❌ incompatible avec la période d'essai"
            )
            can_up = ok_now and not warnings
        elif grade == "confirme":
            previous = previous_week_stats(guild.id).get(member.id, EMPTY_STATS)
            prev_ok = meets(previous, need_money, need_hours)
            lines.append(
                "Semaine précédente : ✅ conditions remplies"
                if prev_ok
                else "Semaine précédente : ❌ régularité sur 2 semaines non démontrée"
            )
            can_up = ok_now and prev_ok
        else:
            lines.append("Entretien interne et validation de la Direction requis.")
            can_up = ok_now
        lines.append(
            "➡️ **Peut monter en grade**" + (
                " (après entretien et validation Direction)" if grade == "senior" else ""
            )
            if can_up
            else "➡️ Ne peut pas encore monter en grade"
        )
    elif grade == "chef":
        lines.append("Grade maximal atteint.")
    else:
        lines.append("Grade non déterminé (rôle Novice / Confirmé / Senior introuvable).")
    return "\n".join(lines)


@bot.tree.command(
    name="info",
    description="Règles et tarifs Taxi Downtown, ou profil d'un employé",
)
@app_commands.describe(
    membre="Profil à consulter; seuls toi-même ou la direction y ont accès"
)
async def info(
    interaction: discord.Interaction, membre: discord.Member | None = None
) -> None:
    if membre is not None:
        if not await require_guild(interaction):
            return
        assert interaction.guild
        if membre.id != interaction.user.id and not is_staff(interaction):
            await interaction.response.send_message(
                "Le profil d'un autre employé est réservé à la direction.",
                ephemeral=True,
            )
            return
        await send_private_text(interaction, employee_profile(interaction.guild, membre))
        return

    embed = discord.Embed(
        title="🚖 Taxi Downtown — Informations",
        description="Entreprise de taxi de Sensity RP.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Tarifs",
        value=(
            "**Ville 700 $/km · Hors ville 1 000 $/km · Cayo 1 300 $/km**\n"
            "Km arrondi au supérieur à partir de x,8 (ex. ville : 1,7 km = 700 $, 1,8 km = 1 400 $)\n"
            f"Attente {FARE_WAIT_PER_MIN} $/min · passager supplémentaire +{FARE_EXTRA_PASSENGER} $ · "
            f"gros bagage +{FARE_BIG_LUGGAGE} $ · réservation à l'avance +{FARE_BOOKING} $\n"
            "Transport médical : 300 à 600 $"
        ),
        inline=False,
    )
    embed.add_field(
        name="Primes hebdomadaires",
        value=(
            "Pas de salaire : primes versées le dimanche soir.\n"
            + " · ".join(
                f"{ordinal(place)} {format_money(prime)}"
                for place, prime in enumerate(WEEKLY_PRIMES, start=1)
            )
            + f"\nMinimum **{MIN_HOURS_FOR_PRIME:g} h** de service. Classement selon l'argent "
            "rapporté, le nombre de courses et l'activité."
        ),
        inline=False,
    )
    embed.add_field(
        name="Règles",
        value=(
            "Prise et fin de service via le panel PDS-FDS · pause de 30 min maximum · "
            "l'entretien du véhicule est à la charge de l'employé."
        ),
        inline=False,
    )
    embed.add_field(
        name="Passages de grade",
        value=(
            "**Novice → Confirmé** : 7 500 $ + 7,5 h, période d'essai (~1 semaine), "
            "aucun avertissement, tarifs et Discord maîtrisés.\n"
            "**Confirmé → Senior** : 8 500 $ + 8,5 h, régularité sur 2 semaines, "
            "bons retours clients, parmi les actifs.\n"
            "**Senior → Formateur & recruteur / Chef de service** : 15 000 $ + 15 h, "
            "entretien interne et validation de la Direction.\n"
            f"Quota {MIN_HOURS_FOR_PRIME:g} h/semaine : sinon avertissement ; "
            "déjà averti = licenciement / démission."
        ),
        inline=False,
    )
    embed.add_field(
        name="Commandes",
        value=(
            "`/course` `/mystats` `/leaderboard` `/tarif`\n"
            "`/myvh` `/checkvh` `/horaire` `/info membre:`"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="tarif", description="Calculer le prix d'une course")
@app_commands.choices(
    zone=[
        app_commands.Choice(name="En ville", value="ville"),
        app_commands.Choice(name="Hors ville", value="hors_ville"),
        app_commands.Choice(name="Cayo", value="cayo"),
    ]
)
@app_commands.describe(
    zone="Zone du trajet",
    km="Distance en kilomètres",
    attente_min="Minutes d'attente",
    passagers_sup="Passagers supplémentaires",
    gros_bagages="Nombre de gros bagages",
    reservation="Réservation à l'avance",
)
async def tarif(
    interaction: discord.Interaction,
    zone: str,
    km: app_commands.Range[float, 0, 500],
    attente_min: app_commands.Range[int, 0, 600] = 0,
    passagers_sup: app_commands.Range[int, 0, 10] = 0,
    gros_bagages: app_commands.Range[int, 0, 20] = 0,
    reservation: bool = False,
) -> None:
    total, lines = compute_fare(km, zone, attente_min, passagers_sup, gros_bagages, reservation)
    await interaction.response.send_message(
        "🧮 **Tarif de la course**\n"
        + "\n".join(f"• {line}" for line in lines)
        + f"\n**Total : {format_money(total)}**",
        ephemeral=True,
    )


# --------------------------------------------------------------------------- #
# Service, courses, statistiques
# --------------------------------------------------------------------------- #

SERVICE_ACTIONS = ("start", "end", "break_start", "break_end")


async def handle_service(interaction: discord.Interaction, action: str) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    try:
        if action == "start":
            settings = STORE.get_settings(interaction.guild.id)
            driver_role_id = settings.get("driver_role_id")
            has_driver_role = bool(
                driver_role_id
                and isinstance(interaction.user, discord.Member)
                and any(role.id == driver_role_id for role in interaction.user.roles)
            )
            if not has_driver_role and STORE.employee_status(
                interaction.guild.id, interaction.user.id
            ) != "active":
                await interaction.response.send_message(
                    "La prise de service est réservée aux chauffeurs recrutés. "
                    "Demande à la direction de t'ajouter au rôle Chauffeur.",
                    ephemeral=True,
                )
                return
            at = STORE.start_service(interaction.guild.id, interaction.user.id)
            message = f"✅ Service pris à **{format_local_time(at)}**. Bonne route !"
            log_message = f"{interaction.user.mention} a pris son service à {format_local_time(at)}."
        elif action == "end":
            result = STORE.end_service(interaction.guild.id, interaction.user.id)
            message = f"✅ Fin de service enregistrée à **{format_local_time(result['ended_at'])}**."
            if result["open_break_seconds"] > MAX_BREAK_SECONDS:
                message += "\n⚠️ La pause en cours dépassait la limite de 30 minutes."
            log_message = f"{interaction.user.mention} a terminé son service."
        elif action == "break_start":
            at = STORE.start_break(interaction.guild.id, interaction.user.id)
            message = f"⏸️ Pause commencée à **{format_local_time(at)}**. Maximum consécutif : 30 minutes."
            log_message = f"{interaction.user.mention} a commencé une pause à {format_local_time(at)}."
        elif action == "break_end":
            seconds = STORE.end_break(interaction.guild.id, interaction.user.id)
            minutes = seconds // 60
            message = f"▶️ Pause terminée après **{minutes} min**."
            if seconds > MAX_BREAK_SECONDS:
                message += "\n⚠️ La limite de 30 minutes a été dépassée."
            log_message = f"{interaction.user.mention} a terminé une pause de {minutes} min."
        else:
            await interaction.response.send_message("Action inconnue.", ephemeral=True)
            return
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(message, ephemeral=True)
    request_pds_refresh(interaction.guild)
    await log_event(interaction.guild, "🕒 Journal de service", log_message)


class PdsView(discord.ui.View):
    """Panel PDS-FDS : boutons persistants de prise/fin de service et de pause."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Prise de service", emoji="🟢", style=discord.ButtonStyle.success,
        custom_id="td:pds:start", row=0,
    )
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await handle_service(interaction, "start")

    @discord.ui.button(
        label="Fin de service", emoji="🔴", style=discord.ButtonStyle.danger,
        custom_id="td:pds:end", row=0,
    )
    async def end(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await handle_service(interaction, "end")

    @discord.ui.button(
        label="Pause", emoji="⏸️", style=discord.ButtonStyle.secondary,
        custom_id="td:pds:break_start", row=1,
    )
    async def pause(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await handle_service(interaction, "break_start")

    @discord.ui.button(
        label="Reprise", emoji="▶️", style=discord.ButtonStyle.secondary,
        custom_id="td:pds:break_end", row=1,
    )
    async def resume(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await handle_service(interaction, "break_end")


def pds_embed(guild_id: int) -> discord.Embed:
    """Panel PDS-FDS avec la liste en direct : 🟢 en service, 🟡 en pause."""
    active = STORE.active_services(guild_id)
    on_break = [row for row in active if row["break_started_at"]]
    on_duty = [row for row in active if not row["break_started_at"]]
    lines = [
        f"🟢 <@{row['user_id']}> — depuis {format_local_time(row['started_at'])[-5:]}"
        for row in on_duty
    ] + [
        f"🟡 <@{row['user_id']}> — en pause depuis "
        f"{format_local_time(row['break_started_at'])[-5:]}"
        for row in on_break
    ]
    shown = lines[:40]
    if len(lines) > len(shown):
        shown.append(f"… et {len(lines) - len(shown)} autre(s)")
    embed = discord.Embed(
        title="🚖 Taxi Downtown — PDS / FDS",
        description=(
            "Utilise les boutons ci-dessous pour journaliser ton service.\n"
            "🟢 **Prise de service** · 🔴 **Fin de service** · "
            "⏸️ **Pause** (30 min max) · ▶️ **Reprise**\n"
            f"Minimum **{MIN_HOURS_FOR_PRIME:g} h** par semaine."
        ),
        color=discord.Color.gold(),
        timestamp=utc_now(),
    )
    embed.add_field(
        name=f"En service ({len(active)})",
        value="\n".join(shown) if shown else "Personne n'est en service.",
        inline=False,
    )
    embed.set_footer(text="Les réponses des boutons te sont envoyées en privé · mis à jour")
    return embed


_pds_refresh_pending: set[int] = set()


async def refresh_pds_panel(guild: discord.Guild) -> None:
    ref = STORE.get_pds_panel(guild.id)
    if not ref:
        return
    channel_id, message_id = ref
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.NotFound:
            STORE.clear_pds_panel(guild.id)
            return
        except (discord.Forbidden, discord.HTTPException):
            return
    try:
        await channel.get_partial_message(message_id).edit(embed=pds_embed(guild.id))  # type: ignore[union-attr]
    except discord.NotFound:
        STORE.clear_pds_panel(guild.id)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        print(f"Impossible de mettre à jour le panel PDS-FDS ({channel_id}).")


def request_pds_refresh(guild: discord.Guild) -> None:
    """Coalesce rapid changes into one edit (Discord rate limits message edits)."""
    if guild.id in _pds_refresh_pending:
        return
    _pds_refresh_pending.add(guild.id)

    async def runner() -> None:
        await asyncio.sleep(1.5)
        _pds_refresh_pending.discard(guild.id)
        try:
            await refresh_pds_panel(guild)
        except Exception as exc:  # le panel ne doit jamais casser une commande
            print(f"Mise à jour du panel PDS-FDS en échec : {type(exc).__name__}.")

    asyncio.create_task(runner())


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #

TICKET_KINDS = {
    "recrutement": ("Candidature", "📋", discord.ButtonStyle.success),
    "support": ("Question / Support", "❓", discord.ButtonStyle.primary),
    "signalement": ("Signalement / Plainte", "🚨", discord.ButtonStyle.danger),
}


class TicketModal(discord.ui.Modal):
    def __init__(self, kind: str) -> None:
        label = TICKET_KINDS[kind][0]
        super().__init__(title=f"Ticket — {label}"[:45])
        self.kind = kind
        self.subject = discord.ui.TextInput(
            label="Sujet / motif",
            style=discord.TextStyle.paragraph,
            min_length=3,
            max_length=500,
            placeholder="Explique ta demande en quelques lignes.",
        )
        self.add_item(self.subject)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Utilise le panel sur le serveur.", ephemeral=True)
            return
        existing = STORE.open_ticket_for(guild.id, interaction.user.id)
        if existing:
            if guild.get_channel(existing["channel_id"]):
                await interaction.response.send_message(
                    f"Tu as déjà un ticket ouvert : <#{existing['channel_id']}>.", ephemeral=True
                )
                return
            STORE.close_ticket(existing["channel_id"])  # salon supprimé à la main
        await interaction.response.defer(ephemeral=True)

        label, emoji, _style = TICKET_KINDS[self.kind]
        direction_id = STORE.get_settings(guild.id).get("direction_role_id")
        direction = guild.get_role(int(direction_id)) if direction_id else None
        member = interaction.user
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                read_message_history=True, embed_links=True,
            ),
        }
        if direction:
            overwrites[direction] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, manage_messages=True,
            )
        slug = re.sub(r"[^a-z0-9-]", "", member.name.lower().replace(" ", "-")) or str(member.id)
        category = getattr(interaction.channel, "category", None)
        try:
            channel = await guild.create_text_channel(
                f"{self.kind}-{slug}"[:90],
                category=category,
                overwrites=overwrites,
                topic=f"Ticket {label} — {member} ({member.id})",
                reason=f"Ticket Taxi Downtown de {member}",
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Impossible de créer le salon : le bot a besoin de la permission **Gérer les salons**.",
                ephemeral=True,
            )
            return
        STORE.create_ticket(guild.id, member.id, channel.id, self.kind, self.subject.value.strip())
        embed = discord.Embed(
            title=f"{emoji} Ticket — {label}",
            description=discord.utils.escape_mentions(self.subject.value.strip()),
            color=discord.Color.gold(),
            timestamp=utc_now(),
        )
        embed.add_field(name="Ouvert par", value=member.mention)
        await channel.send(
            content=f"{member.mention} {direction.mention if direction else ''}".strip(),
            embed=embed,
            view=TicketCloseView(),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=True),
        )
        await interaction.followup.send(f"🎫 Ticket créé : {channel.mention}", ephemeral=True)
        await log_event(
            guild, "🎫 Ticket ouvert", f"{member.mention} — {label} — {channel.mention}"
        )


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        for kind, (label, emoji, style) in TICKET_KINDS.items():
            button = discord.ui.Button(
                label=label, emoji=emoji, style=style, custom_id=f"td:ticket:{kind}"
            )
            button.callback = self._make_callback(kind)
            self.add_item(button)

    @staticmethod
    def _make_callback(kind: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(TicketModal(kind))

        return callback


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Fermer le ticket", emoji="🔒", style=discord.ButtonStyle.danger,
        custom_id="td:ticket:close",
    )
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        channel = interaction.channel
        if interaction.guild is None or channel is None:
            return
        ticket = STORE.ticket_by_channel(channel.id)
        if ticket is None:
            await interaction.response.send_message("Ce ticket est déjà fermé.", ephemeral=True)
            return
        if interaction.user.id != ticket["user_id"] and not is_staff(interaction):
            await interaction.response.send_message(
                "Seuls le demandeur et la direction peuvent fermer ce ticket.", ephemeral=True
            )
            return
        STORE.close_ticket(channel.id)
        await interaction.response.send_message("🔒 Ticket fermé, suppression du salon dans 5 secondes.")
        await log_event(
            interaction.guild,
            "🔒 Ticket fermé",
            f"Ticket de <@{ticket['user_id']}> ({TICKET_KINDS.get(ticket['kind'], ('?',))[0]}) "
            f"fermé par {interaction.user.mention}.\nMotif initial : {ticket['subject'][:500]}",
        )
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket fermé par {interaction.user}")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass


def ticket_embed() -> discord.Embed:
    return discord.Embed(
        title="🎫 Taxi Downtown — Tickets",
        description=(
            "Clique sur le bouton correspondant à ta demande : un salon privé sera créé "
            "avec la direction.\n\n"
            "📋 **Candidature** — rejoindre l'entreprise\n"
            "❓ **Question / Support** — règles, tarifs, véhicules, planning\n"
            "🚨 **Signalement / Plainte** — problème avec un membre ou un client\n\n"
            "Un seul ticket ouvert à la fois."
        ),
        color=discord.Color.gold(),
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def config_embed(guild: discord.Guild) -> discord.Embed:
    settings = STORE.get_settings(guild.id)

    def role(key: str, fallback: str) -> str:
        return f"<@&{settings[key]}>" if settings[key] else fallback

    def channel(key: str) -> str:
        return f"<#{settings[key]}>" if settings[key] else "non configuré"

    embed = discord.Embed(title="⚙️ Configuration Taxi Downtown", color=discord.Color.gold())
    embed.add_field(
        name="Direction", value=role("direction_role_id", "Administrateurs uniquement")
    )
    embed.add_field(name="Chauffeurs", value=role("driver_role_id", "non configuré"))
    embed.add_field(name="Salon des journaux", value=channel("log_channel_id"))
    embed.add_field(name="Salon webhook in-game", value=channel("webhook_channel_id"))
    embed.set_footer(
        text="Sélectionne dans les menus pour modifier. Les boutons publient un panel dans ce salon."
    )
    return embed


class ConfigView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction):
            return True
        await interaction.response.send_message(
            "Seuls les administrateurs peuvent configurer le bot.", ephemeral=True
        )
        return False

    async def _save(self, interaction: discord.Interaction, **values: int) -> None:
        assert interaction.guild
        STORE.set_settings(interaction.guild.id, **values)
        await interaction.response.edit_message(embed=config_embed(interaction.guild), view=self)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Rôle Direction", row=0, min_values=1, max_values=1
    )
    async def pick_direction(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        await self._save(interaction, direction_role_id=select.values[0].id)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Rôle Chauffeur", row=1, min_values=1, max_values=1
    )
    async def pick_driver(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        await self._save(interaction, driver_role_id=select.values[0].id)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Salon des journaux", row=2,
        channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
    )
    async def pick_logs(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._save(interaction, log_channel_id=select.values[0].id)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Salon webhook in-game", row=3,
        channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
    )
    async def pick_webhook(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        await self._save(interaction, webhook_channel_id=select.values[0].id)

    async def _publish(
        self, interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View,
        *, pds: bool = False,
    ) -> None:
        channel = interaction.channel
        try:
            message = await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            await interaction.response.send_message(
                "Je ne peux pas écrire dans ce salon : vérifie mes permissions.", ephemeral=True
            )
            return
        if pds and interaction.guild:
            STORE.set_pds_panel(interaction.guild.id, message.channel.id, message.id)
        await interaction.response.send_message("✅ Panel publié dans ce salon.", ephemeral=True)

    @discord.ui.button(label="Publier PDS-FDS", emoji="🚖", style=discord.ButtonStyle.success, row=4)
    async def publish_pds(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._publish(
            interaction, pds_embed(interaction.guild.id), PdsView(), pds=True  # type: ignore[union-attr]
        )

    @discord.ui.button(label="Publier Tickets", emoji="🎫", style=discord.ButtonStyle.primary, row=4)
    async def publish_tickets(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._publish(interaction, ticket_embed(), TicketPanelView())


@bot.tree.command(name="config", description="Panel de configuration (administrateurs)")
async def config(interaction: discord.Interaction) -> None:
    if not await require_guild(interaction):
        return
    if not is_admin(interaction):
        await interaction.response.send_message(
            "Seuls les administrateurs du serveur peuvent configurer le bot.", ephemeral=True
        )
        return
    assert interaction.guild
    await interaction.response.send_message(
        embed=config_embed(interaction.guild), view=ConfigView(), ephemeral=True
    )


@bot.tree.command(name="course", description="Enregistrer une course effectuée en service")
@app_commands.describe(montant="Montant facturé au client, en dollars")
async def course(
    interaction: discord.Interaction,
    montant: app_commands.Range[int, 1, 100_000],
) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    state = STORE.service_state(interaction.guild.id, interaction.user.id)
    if not state:
        await interaction.response.send_message(
            "Prends ton service avec le panel PDS-FDS avant d'enregistrer une course.",
            ephemeral=True,
        )
        return
    if state["pause"]:
        await interaction.response.send_message(
            "Termine ta pause avec le bouton Reprise du panel PDS-FDS avant d'enregistrer une course.",
            ephemeral=True,
        )
        return
    STORE.record_trip(interaction.guild.id, interaction.user.id, float(montant))
    await interaction.response.send_message(
        f"🚕 Course enregistrée : **{format_money(montant)}**.", ephemeral=True
    )
    await log_event(
        interaction.guild,
        "🚕 Course enregistrée",
        f"{interaction.user.mention} — {format_money(montant)}.",
    )


@bot.tree.command(name="mystats", description="Statistiques et prime estimée de la semaine")
@app_commands.describe(membre="Chauffeur à consulter (direction uniquement)")
async def mystats(
    interaction: discord.Interaction, membre: discord.Member | None = None
) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    target = membre or interaction.user
    if target.id != interaction.user.id and not is_staff(interaction):
        await interaction.response.send_message(
            "Seule la direction peut consulter les statistiques d'un autre chauffeur.",
            ephemeral=True,
        )
        return
    start, _end, stats = weekly_totals(interaction.guild.id)
    state = STORE.service_state(interaction.guild.id, target.id)
    current = "Hors service"
    if state:
        current = "En pause" if state["pause"] else "En service"
    await send_private_text(
        interaction,
        f"📊 **{profile_text(target.display_name)} — semaine depuis le {format_local_time(start)}**\n"
        f"État : {current}\n" + "\n".join(weekly_lines(stats, target.id)),
    )


@bot.tree.command(name="leaderboard", description="Classement des primes de la semaine")
async def leaderboard(interaction: discord.Interaction) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    _start, _end, stats = weekly_totals(interaction.guild.id)
    eligible, others = prime_ranking(stats)
    if not eligible and not others:
        await interaction.response.send_message(
            "Aucune heure n'a encore été enregistrée cette semaine.", ephemeral=True
        )
        return
    medals = ["🥇", "🥈", "🥉"]
    rows = []
    for index, (user_id, hours) in enumerate(eligible[:10]):
        data = stats[user_id]
        prime = WEEKLY_PRIMES[index] if index < len(WEEKLY_PRIMES) else 0
        badge = medals[index] if index < len(medals) else f"**{index + 1}.**"
        rows.append(
            f"{badge} {profile_text(member_name(interaction.guild, user_id))} — "
            f"{format_money(data['fare'])} · {int(data['trips'])} courses · "
            f"{format_duration(hours)}" + (f" → **{format_money(prime)}**" if prime else "")
        )
    if others:
        rows.append(f"\n⏳ **Sous les {MIN_HOURS_FOR_PRIME:g} h requises**")
        rows.extend(
            f"• {profile_text(member_name(interaction.guild, user_id))} — {format_duration(hours)}"
            for user_id, hours in others[:10]
        )
    await interaction.response.send_message(
        "🏆 **Classement des primes — cette semaine**\n" + "\n".join(rows),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="activite", description="Chauffeurs sous le minimum d'heures ou inactifs")
async def activite(interaction: discord.Interaction) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    _start, _end, stats = weekly_totals(interaction.guild.id)
    ids = employee_ids(interaction.guild, stats)
    if not ids:
        await interaction.response.send_message(
            "Aucun chauffeur actif n'est encore enregistré.", ephemeral=True
        )
        return
    below = sorted(
        ((uid, stats.get(uid, EMPTY_STATS)["hours"]) for uid in ids),
        key=lambda item: item[1],
    )
    below = [item for item in below if item[1] < MIN_HOURS_FOR_PRIME]
    recent = STORE.weekly_stats(
        interaction.guild.id, utc_now() - timedelta(days=7), utc_now()
    )
    inactive = sorted(uid for uid in ids if recent.get(uid, EMPTY_STATS)["hours"] <= 0)
    sections = [
        f"**Sous {MIN_HOURS_FOR_PRIME:g} h cette semaine ({len(below)})**",
        *(
            f"• {profile_text(member_name(interaction.guild, uid))} — {format_duration(hours)}"
            for uid, hours in below[:40]
        ),
    ]
    if not below:
        sections.append("✅ Tout le monde a atteint le minimum.")
    sections.append(f"\n**Aucun service depuis 7 jours ({len(inactive)})**")
    sections.extend(
        f"• {profile_text(member_name(interaction.guild, uid))}" for uid in inactive[:40]
    )
    if not inactive:
        sections.append("✅ Tous les chauffeurs ont roulé cette semaine.")
    await send_private_text(interaction, "\n".join(sections))


# --------------------------------------------------------------------------- #
# Flotte
# --------------------------------------------------------------------------- #


async def plate_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    if not interaction.guild:
        return []
    needle = current.strip().upper()
    return [
        app_commands.Choice(name=f"{row['plate']} — {row['model']}"[:100], value=row["plate"])
        for row in STORE.all_vehicles(interaction.guild.id)
        if needle in row["plate"] or needle in row["model"].upper()
    ][:25]


def vehicle_line(row: dict[str, Any]) -> str:
    owner = f"<@{row['assigned_user_id']}>" if row.get("assigned_user_id") else "non attribué"
    return f"• **{profile_text(row['plate'])}** — {profile_text(row['model'])} — {owner}"


@bot.tree.command(name="addvh", description="Ajouter un véhicule à la flotte")
@app_commands.describe(
    plaque="Plaque d'immatriculation",
    modele="Modèle du véhicule",
    chauffeur="Chauffeur auquel attribuer le véhicule",
)
async def addvh(
    interaction: discord.Interaction,
    plaque: app_commands.Range[str, 1, 20],
    modele: app_commands.Range[str, 1, 80],
    chauffeur: discord.Member | None = None,
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    plate = plaque.strip().upper()
    try:
        STORE.add_vehicle(
            interaction.guild.id, plate, modele.strip(), chauffeur.id if chauffeur else None
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    assignment = f" attribué à {chauffeur.mention}" if chauffeur else " sans attribution"
    await interaction.response.send_message(
        f"🚗 Véhicule **{plate} — {modele}** ajouté{assignment}.", ephemeral=True
    )
    await log_event(
        interaction.guild,
        "🚗 Véhicule ajouté",
        f"{interaction.user.mention} a ajouté {plate} — {modele}{assignment}.",
    )


@bot.tree.command(name="delvh", description="Retirer un véhicule de la flotte")
@app_commands.autocomplete(plaque=plate_autocomplete)
async def delvh(
    interaction: discord.Interaction, plaque: app_commands.Range[str, 1, 20]
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    vehicle = STORE.delete_vehicle(interaction.guild.id, plaque.strip().upper())
    if not vehicle:
        await interaction.response.send_message("Véhicule introuvable.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"🗑️ Le véhicule **{vehicle['plate']} — {vehicle['model']}** a été retiré.",
        ephemeral=True,
    )
    await log_event(
        interaction.guild,
        "🗑️ Véhicule retiré",
        f"{interaction.user.mention} a retiré {vehicle['plate']} — {vehicle['model']}.",
    )


@bot.tree.command(name="assignvh", description="Attribuer ou libérer un véhicule")
@app_commands.describe(
    plaque="Plaque du véhicule",
    chauffeur="Nouveau chauffeur (laisser vide pour libérer le véhicule)",
)
@app_commands.autocomplete(plaque=plate_autocomplete)
async def assignvh(
    interaction: discord.Interaction,
    plaque: app_commands.Range[str, 1, 20],
    chauffeur: discord.Member | None = None,
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    vehicle = STORE.assign_vehicle(
        interaction.guild.id, plaque.strip().upper(), chauffeur.id if chauffeur else None
    )
    if not vehicle:
        await interaction.response.send_message("Véhicule introuvable.", ephemeral=True)
        return
    target = f"attribué à {chauffeur.mention}" if chauffeur else "libéré"
    await interaction.response.send_message(
        f"🔑 **{vehicle['plate']}** {target}.", ephemeral=True
    )
    await log_event(
        interaction.guild,
        "🔑 Attribution de véhicule",
        f"{interaction.user.mention} : {vehicle['plate']} {target}.",
    )


@bot.tree.command(name="checkvh", description="Consulter un véhicule, ou toute la flotte (direction)")
@app_commands.describe(plaque="Plaque à consulter; vide = liste complète (direction)")
@app_commands.autocomplete(plaque=plate_autocomplete)
async def checkvh(
    interaction: discord.Interaction,
    plaque: app_commands.Range[str, 1, 20] | None = None,
) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    if plaque is None:
        if not await require_staff(interaction):
            return
        rows = STORE.all_vehicles(interaction.guild.id)
        text = (
            f"**Flotte ({len(rows)} véhicules)**\n" + "\n".join(vehicle_line(r) for r in rows)
            if rows
            else "Aucun véhicule n'est enregistré."
        )
        await send_private_text(interaction, text)
        return
    vehicle = STORE.get_vehicle(interaction.guild.id, plaque.strip().upper())
    if not vehicle:
        await interaction.response.send_message(
            "Aucun véhicule trouvé pour cette plaque.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        vehicle_line(vehicle),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(name="myvh", description="Afficher les véhicules qui te sont attribués")
async def myvh(interaction: discord.Interaction) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    rows = STORE.vehicles_for_user(interaction.guild.id, interaction.user.id)
    text = (
        "🔑 **Tes véhicules**\n"
        + "\n".join(f"• {row['plate']} — {row['model']}" for row in rows)
        if rows
        else "🔑 Aucun véhicule ne t'est actuellement attribué."
    )
    await interaction.response.send_message(text, ephemeral=True)


# --------------------------------------------------------------------------- #
# Ressources humaines
# --------------------------------------------------------------------------- #


@bot.tree.command(name="recruter", description="Accepter ou refuser une candidature")
@app_commands.describe(
    membre="Candidat ou chauffeur concerné",
    decision="Accepter pour l'intégrer ou refuser la candidature",
)
@app_commands.choices(
    decision=[
        app_commands.Choice(name="Accepter", value="accept"),
        app_commands.Choice(name="Refuser", value="reject"),
    ]
)
async def recruter(
    interaction: discord.Interaction,
    membre: discord.Member,
    decision: str,
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    if membre.bot:
        await interaction.response.send_message(
            "Une candidature doit concerner un membre humain.", ephemeral=True
        )
        return
    if decision == "accept":
        STORE.set_employee_status(interaction.guild.id, membre.id, "active")
        settings = STORE.get_settings(interaction.guild.id)
        roles_to_grant = [
            role for role in recruitment_roles(interaction.guild, settings) if role not in membre.roles
        ]
        role_note = ""
        if roles_to_grant:
            try:
                await membre.add_roles(
                    *roles_to_grant,
                    reason=f"Recrutement Taxi Downtown par {interaction.user}",
                )
                role_note = " Rôles attribués : " + ", ".join(
                    role.mention for role in roles_to_grant
                ) + "."
            except discord.Forbidden:
                role_note = (
                    " ⚠️ Le bot n'a pas la permission d'attribuer un ou plusieurs rôles "
                    "(vérifie `Gérer les rôles` et la hiérarchie)."
                )
            except discord.HTTPException:
                role_note = " ⚠️ L'attribution des rôles a échoué côté Discord."
        elif not recruitment_roles(interaction.guild, settings):
            role_note = (
                " ⚠️ Aucun rôle de recrutement trouvé. Crée les rôles `Taxi Downtown`, "
                "`Chauffeur` et `Novice`, ou configure le rôle Chauffeur avec `/config`."
            )
        message = f"✅ {membre.mention} a été recruté(e) chez Taxi Downtown.{role_note}"
        log_message = f"{interaction.user.mention} a accepté la candidature de {membre.mention}."
    else:
        STORE.set_employee_status(interaction.guild.id, membre.id, "rejected")
        message = f"❌ La candidature de {membre.mention} a été refusée."
        log_message = f"{interaction.user.mention} a refusé la candidature de {membre.mention}."
    await interaction.response.send_message(
        message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )
    await log_event(interaction.guild, "📋 Décision de recrutement", log_message)


@bot.tree.command(name="averto", description="Ajouter un avertissement à un chauffeur")
async def averto(
    interaction: discord.Interaction,
    membre: discord.Member,
    motif: app_commands.Range[str, 3, 500],
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    warning_id = STORE.add_warning(
        interaction.guild.id, membre.id, motif.strip(), interaction.user.id
    )
    active_count = len(STORE.warnings_for_user(interaction.guild.id, membre.id))
    await interaction.response.send_message(
        f"⚠️ Avertissement **#{warning_id}** ajouté à {membre.mention}. "
        f"Avertissements actifs : {active_count}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await log_event(
        interaction.guild,
        "⚠️ Avertissement RH",
        f"Avertissement #{warning_id} pour {membre.mention} par {interaction.user.mention}.\n"
        f"Motif : {motif.strip()}",
    )


@bot.tree.command(name="delaverto", description="Retirer un avertissement RH")
@app_commands.describe(id_avertissement="ID à retirer (facultatif, dernier actif par défaut)")
async def delaverto(
    interaction: discord.Interaction,
    membre: discord.Member,
    id_avertissement: int | None = None,
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    row = STORE.remove_warning(
        interaction.guild.id, membre.id, interaction.user.id, id_avertissement
    )
    if not row:
        await interaction.response.send_message(
            "Aucun avertissement actif correspondant n'a été trouvé.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"✅ Avertissement **#{row['id']}** retiré pour {membre.mention}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await log_event(
        interaction.guild,
        "✅ Avertissement retiré",
        f"{interaction.user.mention} a retiré l'avertissement #{row['id']} de {membre.mention}.",
    )


@bot.tree.command(name="kickj", description="Retirer un membre de l'équipe Taxi Downtown")
async def kickj(
    interaction: discord.Interaction,
    membre: discord.Member,
    motif: app_commands.Range[str, 3, 300],
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    settings = STORE.get_settings(interaction.guild.id)
    role_id = settings.get("driver_role_id")
    role = interaction.guild.get_role(int(role_id)) if role_id else None
    if role and role in membre.roles:
        try:
            await membre.remove_roles(
                role, reason=f"Retrait de l'équipe Taxi Downtown par {interaction.user}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Le bot ne peut pas retirer ce rôle. Vérifie sa permission et la hiérarchie des rôles.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Discord n'a pas pu retirer le rôle Chauffeur.", ephemeral=True
            )
            return
    STORE.set_employee_status(interaction.guild.id, membre.id, "inactive")
    try:  # un service resté ouvert fausserait les heures et le classement
        STORE.end_service(interaction.guild.id, membre.id)
        request_pds_refresh(interaction.guild)
    except ValueError:
        pass
    released = STORE.vehicles_for_user(interaction.guild.id, membre.id)
    for vehicle in released:
        STORE.assign_vehicle(interaction.guild.id, vehicle["plate"], None)
    vehicle_note = (
        f" {len(released)} véhicule(s) libéré(s)." if released else ""
    )
    await interaction.response.send_message(
        f"🚕 {membre.mention} a été retiré(e) de l'équipe Taxi Downtown.{vehicle_note} "
        "Cette commande ne l'expulse pas du serveur Discord.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await log_event(
        interaction.guild,
        "🚕 Retrait de l'équipe",
        f"{membre.mention} retiré(e) par {interaction.user.mention}.\nMotif : {motif.strip()}",
    )


@bot.tree.command(name="payerprime", description="Enregistrer le versement d'une prime à un chauffeur")
@app_commands.describe(
    membre="Le chauffeur récompensé", 
    montant="Montant de la prime versée", 
    raison="Motif (ex: 1ère place semaine 43)"
)
async def payerprime(
    interaction: discord.Interaction, 
    membre: discord.Member, 
    montant: app_commands.Range[int, 1, 100_000], 
    raison: app_commands.Range[str, 3, 150] = "Prime hebdomadaire"
) -> None:
    if not await require_staff(interaction):
        return
    assert interaction.guild
    
    STORE.record_payout(interaction.guild.id, membre.id, float(montant), raison)
    
    embed = discord.Embed(title="💸 Prime versée", color=discord.Color.green())
    embed.add_field(name="Chauffeur", value=membre.mention, inline=True)
    embed.add_field(name="Montant", value=f"**{format_money(montant)}**", inline=True)
    embed.add_field(name="Motif", value=raison, inline=False)
    
    await interaction.response.send_message(
        f"✅ Prime enregistrée pour {membre.mention}.", 
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )
    await log_event(
        interaction.guild, 
        "💸 Versement de prime", 
        f"{interaction.user.mention} a versé **{format_money(montant)}** à {membre.mention}.\nMotif : {raison}"
    )


@bot.tree.command(name="horaire", description="Consulter ou modifier un planning de chauffeur")
@app_commands.describe(
    membre="Membre concerné (direction uniquement pour consulter les autres)",
    planning="Texte du planning à enregistrer (direction uniquement)",
)
async def horaire(
    interaction: discord.Interaction,
    membre: discord.Member | None = None,
    planning: app_commands.Range[str, 1, 1500] | None = None,
) -> None:
    if not await require_guild(interaction):
        return
    assert interaction.guild
    target = membre or interaction.user
    if planning is not None:
        if not is_staff(interaction):
            await interaction.response.send_message(
                "Seule la direction peut modifier un planning.", ephemeral=True
            )
            return
        STORE.set_schedule(interaction.guild.id, target.id, planning.strip(), interaction.user.id)
        await interaction.response.send_message(
            f"🗓️ Planning enregistré pour {target.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await log_event(
            interaction.guild,
            "🗓️ Planning modifié",
            f"Planning de {target.mention} modifié par {interaction.user.mention}.",
        )
        return
    if target.id != interaction.user.id and not is_staff(interaction):
        await interaction.response.send_message(
            "Tu peux consulter ton propre planning; la direction peut consulter celui des autres.",
            ephemeral=True,
        )
        return
    row = STORE.get_schedule(interaction.guild.id, target.id)
    text = (
        f"🗓️ **Planning de {profile_text(target.display_name)}**\n{profile_text(row['schedule_text'])}"
        if row
        else f"Aucun planning n'est enregistré pour {profile_text(target.display_name)}."
    )
    await send_private_text(interaction, text)

# --------------------------------------------------------------------------- #
# Ouverture / Fermeture du service
# --------------------------------------------------------------------------- #

# Remplacez ces liens par vos propres images si vous le souhaitez
IMAGE_OUVERT = "https://i.postimg.cc/bN2rhrhh/image.png"
IMAGE_FERMER = "https://i.postimg.cc/qBNyNkT3/Taxi-Ferme.png"

@bot.tree.command(name="ouvert", description="Ouvre le service Taxi Downtown et prévient les citoyens")
async def ouvert(interaction: discord.Interaction):
    # Réservé à la direction (ou aux chauffeurs selon votre choix, ici require_staff)
    if not await require_staff(interaction):
        return
    assert interaction.guild
    
    # Recherche du rôle Citoyen pour faire un vrai ping
    citoyen_role = discord.utils.get(interaction.guild.roles, name="Citoyen")
    ping = citoyen_role.mention if citoyen_role else "@Citoyen"
    
    embed = discord.Embed(
        title="🚖 Taxi Downtown — OUVERT 🟢",
        description="Le service est désormais **ouvert** !\n\nNos chauffeurs sont prêts à vous conduire où vous le souhaitez. N'hésitez pas à nous contacter en jeu ou sur Discord.",
        color=discord.Color.green(),
        timestamp=utc_now()
    )
    embed.set_image(url=IMAGE_OUVERT)
    embed.set_footer(text="Taxi Downtown - Toujours à votre service")
    
    await interaction.response.send_message(
        content=f"{ping} 📢 **Le taxi est ouvert !**",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    
    await log_event(
        interaction.guild,
        "🟢 Ouverture du service",
        f"Le service a été ouvert par {interaction.user.mention}."
    )


@bot.tree.command(name="fermer", description="Ferme le service Taxi Downtown et prévient les citoyens")
async def fermer(interaction: discord.Interaction):
    # Réservé à la direction
    if not await require_staff(interaction):
        return
    assert interaction.guild
    
    # Recherche du rôle Citoyen pour faire un vrai ping
    citoyen_role = discord.utils.get(interaction.guild.roles, name="Citoyen")
    ping = citoyen_role.mention if citoyen_role else "@Citoyen"
    
    embed = discord.Embed(
        title="🚖 Taxi Downtown — FERMÉ 🔴",
        description="Le service est actuellement **fermé**.\n\nNous vous remercions de votre confiance et nous vous retrouverons très prochainement.",
        color=discord.Color.red(),
        timestamp=utc_now()
    )
    embed.set_image(url=IMAGE_FERMER)
    embed.set_footer(text="Taxi Downtown - À bientôt")
    
    await interaction.response.send_message(
        content=f"{ping} 📢 **Le taxi est fermé.**",
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    
    await log_event(
        interaction.guild,
        "🔴 Fermeture du service",
        f"Le service a été fermé par {interaction.user.mention}."
    )

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        message = "Cette commande vient d'être utilisée. Réessaie dans quelques secondes."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "Tu n'as pas les permissions Discord nécessaires."
    else:
        print(f"Erreur de commande {interaction.command}: {type(error).__name__}: {error}")
        message = "La commande a échoué. Réessaie ou contacte la direction."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("❌ DISCORD_TOKEN n'est pas configuré dans les Secrets Replit.")
    bot.run(token)


if __name__ == "__main__":
    main()
