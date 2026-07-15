from __future__ import annotations

import discord


class SbugaView(discord.ui.View):
    """Base view: tracks its message and disables itself on timeout."""

    def __init__(
        self, *, timeout: float | None = 180, restrict_to: int | None = None
    ) -> None:
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None
        self.restrict_to = restrict_to
        # items the last _disable_all() turned off, so _enable_all() restores exactly those
        self._prev_enabled: list[discord.ui.Button | discord.ui.Select] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.restrict_to is not None and interaction.user.id != self.restrict_to:
            await interaction.response.send_message(
                "You can't interact with this — run the command yourself.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        """Disable every button/select, remembering which were enabled beforehand so a paired
        _enable_all() re-enables exactly those (leaving already-disabled ones disabled).
        """
        self._prev_enabled = [
            item
            for item in self.children
            if isinstance(item, (discord.ui.Button, discord.ui.Select))
            and not item.disabled
        ]
        for item in self._prev_enabled:
            item.disabled = True

    def _enable_all(self) -> None:
        """Re-enable only the items the last _disable_all() disabled."""
        for item in self._prev_enabled:
            item.disabled = False
        self._prev_enabled = []

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LinkButtonView(SbugaView):
    """A non-expiring view holding one or more link buttons."""

    def __init__(self, buttons: list[tuple[str, str]]) -> None:
        super().__init__(timeout=None)
        for label, url in buttons:
            self.add_item(discord.ui.Button(label=label, url=url))


class Paginator(SbugaView):
    """Generic prev/next paginator driven by a render(page) -> Embed callable."""

    def __init__(
        self, render, total_pages: int, restriction_id: int, *, timeout: float = 180
    ) -> None:
        super().__init__(timeout=timeout, restrict_to=restriction_id)
        self.render = render
        self.total_pages = max(1, total_pages)
        self.current_page = 1
        self._update()

    def _update(self) -> None:
        self.previous_page.disabled = self.current_page == 1
        self.next_page.disabled = self.current_page == self.total_pages

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.primary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page > 1:
            self.current_page -= 1
        self._update()
        await interaction.response.edit_message(
            embed=self.render(self.current_page), view=self
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.current_page < self.total_pages:
            self.current_page += 1
        self._update()
        await interaction.response.edit_message(
            embed=self.render(self.current_page), view=self
        )
