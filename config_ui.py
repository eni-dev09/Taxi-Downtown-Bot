"""Interface /config : tableau de bord à catégories, modules activables et champs.

Écrans (Components V2, message privé) : accueil -> catégorie -> module.
Les valeurs sont stockées via features.py ; les actions (publier un panel)
sont fournies par bot.py grâce à setup() pour éviter tout import circulaire.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import discord

import features
from features import CATEGORIES, CATEGORY_BY_KEY, FIELDS, MODULES, Category, Field, GuildConfig, Module

Action = Callable[[discord.Interaction], Awaitable[None]]
Logger = Callable[[discord.Guild, str, str], Awaitable[Any]]

GOLD = discord.Colour.gold()
STATE_ICON = {"on": "✅", "todo": "⚠️", "off": "🔴"}
STATE_LABEL = {"on": "Activé", "todo": "À compléter", "off": "Désactivé"}

_store: Any = None
_actions: dict[str, Action] = {}
_logger: Logger | None = None


def setup(store: Any, actions: dict[str, Action], logger: Logger | None = None) -> None:
    global _store, _actions, _logger
    _store, _actions, _logger = store, actions, logger


# --------------------------------------------------------------------------- #
# Affichage des valeurs
# --------------------------------------------------------------------------- #


def format_field_value(field: Field, cfg: GuildConfig) -> str:
    value = cfg.get(field.key)
    if value in (None, ""):
        return "*non défini*"
    if field.kind == "role":
        return f"<@&{value}>"
    if field.kind in ("channel", "category"):
        return f"<#{value}>"
    if field.kind == "int":
        return f"`{value:,}`".replace(",", " ")
    if field.kind == "float":
        return f"`{value:g}`"
    text = str(value)
    return f"`{text[:60]}…`" if len(text) > 60 else f"`{text}`"


def module_summary(module: Module, cfg: GuildConfig) -> str:
    text = module.description
    return text if len(text) <= 90 else text[:87] + "…"


# --------------------------------------------------------------------------- #
# Vues
# --------------------------------------------------------------------------- #


class _AdminView(discord.ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=900)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if (
            interaction.guild
            and isinstance(user, discord.Member)
            and user.guild_permissions.administrator
        ):
            return True
        await interaction.response.send_message(
            "Seuls les administrateurs peuvent modifier la configuration.", ephemeral=True
        )
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: Any) -> None:
        print(f"Erreur /config : {type(error).__name__}: {error}")
        message = "Une erreur est survenue dans le panel de configuration. Réessaie."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _button(
    label: str,
    callback: Callable[[discord.Interaction], Awaitable[None]],
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: str | None = None,
) -> discord.ui.Button:
    button = discord.ui.Button(label=label, style=style, emoji=emoji)
    button.callback = callback  # type: ignore[method-assign]
    return button


async def _show(interaction: discord.Interaction, view: discord.ui.LayoutView) -> None:
    await interaction.response.edit_message(view=view)


async def _audit(interaction: discord.Interaction, text: str) -> None:
    if _logger and interaction.guild:
        await _logger(interaction.guild, "⚙️ Configuration modifiée", f"{interaction.user.mention} — {text}")


def home_view(guild: discord.Guild) -> discord.ui.LayoutView:
    cfg = GuildConfig.load(_store, guild.id)
    view = _AdminView()
    header = discord.ui.TextDisplay(
        f"## 🚖 {guild.name}\n-# Configuration du bot Taxi Downtown · choisis une catégorie"
    )
    children: list[Any] = []
    icon = getattr(guild, "icon", None)
    if icon is not None:
        children.append(discord.ui.Section(header, accessory=discord.ui.Thumbnail(icon.url)))
    else:
        children.append(header)
    children.append(discord.ui.Separator())

    todo = 0
    for category in CATEGORIES:
        ready, total = cfg.category_counts(category)
        todo += sum(1 for m in category.modules if cfg.state(m) == "todo")

        async def open_category(interaction: discord.Interaction, key: str = category.key) -> None:
            await _show(interaction, category_view(interaction.guild, CATEGORY_BY_KEY[key]))

        children.append(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"**{category.emoji} {category.label}** · `{ready}/{total}`\n-# {category.description}"
                ),
                accessory=_button("Ouvrir", open_category),
            )
        )
    if todo:
        children.append(discord.ui.Separator())
        children.append(
            discord.ui.TextDisplay(f"-# ⚠️ {todo} module(s) à compléter (rôle ou salon manquant).")
        )
    view.add_item(discord.ui.Container(*children, accent_colour=GOLD))
    return view


def category_view(guild: discord.Guild, category: Category) -> discord.ui.LayoutView:
    cfg = GuildConfig.load(_store, guild.id)
    ready, total = cfg.category_counts(category)
    view = _AdminView()
    children: list[Any] = [
        discord.ui.TextDisplay(
            f"## {category.emoji} {category.label} · `{ready}/{total}`\n-# {category.description}"
        ),
        discord.ui.Separator(),
    ]
    for module in category.modules:
        state = cfg.state(module)

        async def open_module(interaction: discord.Interaction, key: str = module.key) -> None:
            await _show(interaction, module_view(interaction.guild, MODULES[key]))

        children.append(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    f"{STATE_ICON[state]} **{module.emoji} {module.label}**\n-# {module_summary(module, cfg)}"
                ),
                accessory=_button("Ouvrir", open_module),
            )
        )

    async def back(interaction: discord.Interaction) -> None:
        await _show(interaction, home_view(interaction.guild))

    children.append(discord.ui.Separator())
    children.append(discord.ui.ActionRow(_button("← Accueil", back)))
    view.add_item(discord.ui.Container(*children, accent_colour=GOLD))
    return view


class ValuesModal(discord.ui.Modal):
    """Édition des champs nombre/texte d'un module (5 champs maximum)."""

    def __init__(self, module: Module, cfg: GuildConfig) -> None:
        super().__init__(title=f"{module.label}"[:45])
        self.module = module
        self.fields_by_input: list[tuple[Field, discord.ui.TextInput]] = []
        for field in _text_fields(module):
            current = cfg.get(field.key)
            hint = ""
            if field.minimum is not None or field.maximum is not None:
                hint = f"{field.minimum:g} à {field.maximum:g}" if field.maximum is not None else ""
            text_input = discord.ui.TextInput(
                label=field.label[:45],
                default="" if current in (None, "") else f"{current:g}" if isinstance(current, float) else str(current),
                placeholder=(hint or "vide = valeur par défaut")[:100],
                required=field.kind != "text",
                max_length=400,
            )
            self.add_item(text_input)
            self.fields_by_input.append((field, text_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed: list[tuple[Field, Any]] = []
        errors: list[str] = []
        for field, text_input in self.fields_by_input:
            try:
                parsed.append((field, features.parse_value(field, text_input.value)))
            except ValueError as exc:
                errors.append(f"• {exc}")
        if errors:
            await interaction.response.send_message(
                "❌ Aucune valeur enregistrée :\n" + "\n".join(errors), ephemeral=True
            )
            return
        assert interaction.guild
        for field, value in parsed:
            features.set_field(_store, interaction.guild.id, field, value)
        await _audit(
            interaction,
            f"**{self.module.label}** : "
            + ", ".join(f"{f.label} = {v if v is not None else 'défaut'}" for f, v in parsed),
        )
        await _show(interaction, module_view(interaction.guild, self.module))


def _text_fields(module: Module) -> list[Field]:
    return [f for f in module.fields if f.kind in ("int", "float", "text")]


def _picker_fields(module: Module) -> list[Field]:
    return [f for f in module.fields if f.kind in ("role", "channel", "category")]


def _make_picker(
    guild: discord.Guild, module: Module, field: Field, cfg: GuildConfig
) -> discord.ui.Item:
    current = cfg.get(field.key)
    # Une valeur présélectionnée doit exister sur le serveur (rôle/salon supprimé = vide).
    exists = guild.get_role(current) if field.kind == "role" else guild.get_channel(current)
    if current and exists is None:
        current = None
    if field.kind == "role":
        defaults = (
            [discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.role)]
            if current
            else []
        )
        select: Any = discord.ui.RoleSelect(
            placeholder=f"{field.label}…"[:150], min_values=1, max_values=1, default_values=defaults
        )
    else:
        channel_types = (
            [discord.ChannelType.category]
            if field.kind == "category"
            else [discord.ChannelType.text, discord.ChannelType.news]
        )
        defaults = (
            [discord.SelectDefaultValue(id=current, type=discord.SelectDefaultValueType.channel)]
            if current
            else []
        )
        select = discord.ui.ChannelSelect(
            placeholder=f"{field.label}…"[:150],
            channel_types=channel_types,
            min_values=1,
            max_values=1,
            default_values=defaults,
        )

    async def picked(interaction: discord.Interaction) -> None:
        assert interaction.guild
        chosen = select.values[0]
        features.set_field(_store, interaction.guild.id, field, chosen.id)
        await _audit(interaction, f"**{module.label}** : {field.label} = <#{chosen.id}>"
                     if field.kind != "role" else f"**{module.label}** : {field.label} = <@&{chosen.id}>")
        await _show(interaction, module_view(interaction.guild, module))

    select.callback = picked
    return select


def module_view(guild: discord.Guild, module: Module) -> discord.ui.LayoutView:
    cfg = GuildConfig.load(_store, guild.id)
    category = features.CATEGORY_OF[module.key]
    state = cfg.state(module)
    view = _AdminView()

    children: list[Any] = [
        discord.ui.TextDisplay(f"## {module.emoji} {module.label}\n{module.description}"),
        discord.ui.TextDisplay(f"**État :** {STATE_ICON[state]} {STATE_LABEL[state]}"),
        discord.ui.Separator(),
    ]
    for field in _picker_fields(module):
        children.append(discord.ui.TextDisplay(f"**{field.label}** : {format_field_value(field, cfg)}"))
        children.append(discord.ui.ActionRow(_make_picker(guild, module, field, cfg)))

    text_fields = _text_fields(module)
    if text_fields:
        async def edit_values(interaction: discord.Interaction) -> None:
            assert interaction.guild
            await interaction.response.send_modal(
                ValuesModal(module, GuildConfig.load(_store, interaction.guild.id))
            )

        children.append(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "\n".join(f"**{f.label}** : {format_field_value(f, cfg)}" for f in text_fields)
                ),
                accessory=_button("Modifier", edit_values, discord.ButtonStyle.primary, "✏️"),
            )
        )

    buttons: list[discord.ui.Button] = []
    if module.toggle:
        enabled = cfg.enabled(module.key)

        async def toggle(interaction: discord.Interaction) -> None:
            assert interaction.guild
            now = not GuildConfig.load(_store, interaction.guild.id).enabled(module.key)
            features.set_enabled(_store, interaction.guild.id, module.key, now)
            await _audit(interaction, f"**{module.label}** {'activé' if now else 'désactivé'}")
            await _show(interaction, module_view(interaction.guild, module))

        buttons.append(
            _button(
                "Désactiver" if enabled else "Activer",
                toggle,
                discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            )
        )
    for action_id, label in module.actions:

        async def run_action(interaction: discord.Interaction, key: str = action_id) -> None:
            handler = _actions.get(key)
            if handler is None:
                await interaction.response.send_message("Action indisponible.", ephemeral=True)
                return
            await handler(interaction)

        buttons.append(_button(label, run_action, discord.ButtonStyle.primary, "📌"))
    if module.fields:

        async def reset(interaction: discord.Interaction) -> None:
            assert interaction.guild
            features.reset_module(_store, interaction.guild.id, module)
            await _audit(interaction, f"**{module.label}** réinitialisé")
            await _show(interaction, module_view(interaction.guild, module))

        buttons.append(_button("Réinitialiser", reset))

    async def back(interaction: discord.Interaction) -> None:
        await _show(interaction, category_view(interaction.guild, category))

    buttons.append(_button("← Retour", back))
    children.append(discord.ui.Separator())
    children.append(discord.ui.ActionRow(*buttons))
    view.add_item(discord.ui.Container(*children, accent_colour=GOLD))
    return view
