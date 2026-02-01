import random

import discord
from discord.ext import commands, tasks

from utils import config, db


GUILD_ID = config.secrets["discord"]["guild_id"]


# Database helper functions for predictions
async def create_prediction_db(
    creator_id, title, option_a, option_b, thread_id, message_id
):
    """Insert a new prediction and return its ID."""
    sql = """
        INSERT INTO predictions (creator_id, title, option_a, option_b, thread_id, message_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id;
    """
    result = await db.fetch_one(
        sql, [creator_id, title, option_a, option_b, thread_id, message_id]
    )
    return result[0]


async def get_active_prediction_by_creator(creator_id):
    """Check if creator has an open (active or locked) prediction."""
    sql = """
        SELECT id FROM predictions
        WHERE creator_id = %s AND status IN ('active', 'locked')
        LIMIT 1;
    """
    return await db.fetch_one(sql, [creator_id])


async def update_prediction_status(prediction_id, status, winner=None):
    """Update prediction status and optionally set the winner."""
    if winner:
        sql = "UPDATE predictions SET status = %s, winner = %s WHERE id = %s;"
        await db.perform_one(sql, [status, winner, prediction_id])
    else:
        sql = "UPDATE predictions SET status = %s WHERE id = %s;"
        await db.perform_one(sql, [status, prediction_id])


async def place_bet_db(prediction_id, user_id, option, points):
    """Insert or update a bet using UPSERT."""
    sql = """
        INSERT INTO prediction_bets (prediction_id, user_id, option, points)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (prediction_id, user_id)
        DO UPDATE SET points = prediction_bets.points + EXCLUDED.points;
    """
    await db.perform_one(sql, [prediction_id, user_id, option, points])


async def get_bets_for_prediction(prediction_id):
    """Get all bets for a prediction."""
    sql = (
        "SELECT user_id, option, points FROM prediction_bets WHERE prediction_id = %s;"
    )
    return await db.fetch_all(sql, [prediction_id])


async def get_restorable_predictions():
    """Get all active or locked predictions for startup restoration."""
    sql = """
        SELECT id, creator_id, title, option_a, option_b, status, thread_id, message_id
        FROM predictions
        WHERE status IN ('active', 'locked');
    """
    return await db.fetch_all(sql, [])


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.points_buffer = {}
        self.predictions = {}
        self.update_points.start()

    points = discord.SlashCommandGroup("points", "points :)")
    points_prediction = points.create_subgroup("prediction", "Predictions with points")

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore predictions from database on bot startup."""
        await self.restore_predictions()

    async def restore_predictions(self):
        """Restore active/locked predictions from the database."""
        predictions_data = await get_restorable_predictions()

        for row in predictions_data:
            (
                db_id,
                creator_id,
                title,
                option_a,
                option_b,
                status,
                thread_id,
                message_id,
            ) = row

            # Try to get the thread
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except discord.NotFound:
                # Thread was deleted, refund all bets
                await self.refund_prediction_bets(db_id)
                await update_prediction_status(db_id, "refunded")
                continue
            except discord.Forbidden:
                # No access to thread, mark as refunded
                await self.refund_prediction_bets(db_id)
                await update_prediction_status(db_id, "refunded")
                continue

            # Get existing bets
            bets = await get_bets_for_prediction(db_id)
            option_a_points = {}
            option_b_points = {}
            for user_id, option, points in bets:
                if option == option_a:
                    option_a_points[user_id] = points
                else:
                    option_b_points[user_id] = points

            # Recreate the prediction
            prediction = Prediction.from_database(
                db_id,
                title,
                option_a,
                option_b,
                thread,
                message_id,
                option_a_points,
                option_b_points,
                status == "locked",
            )
            await prediction.restore_view()

            self.predictions[creator_id] = prediction

    async def refund_prediction_bets(self, prediction_id):
        """Refund all bets for a prediction."""
        bets = await get_bets_for_prediction(prediction_id)
        if not bets:
            return

        sql = "UPDATE users SET points = points + %s WHERE discordid = %s;"
        data = [(points, user_id) for user_id, _, points in bets]
        await db.perform_many(sql, data)

    @commands.Cog.listener()
    async def on_message(self, message):
        user = message.author
        if user == self.bot.user or user.bot:
            return

        self.points_buffer[user.id] = random.randint(7, 25)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread):
        """Handle thread deletion - refund bets if prediction thread is deleted."""
        # Find if any prediction uses this thread
        for creator_id, prediction in list(self.predictions.items()):
            if prediction.thread.id == thread.id:
                await self.refund_prediction_bets(prediction.db_id)
                await update_prediction_status(prediction.db_id, "refunded")
                del self.predictions[creator_id]
                break

    @points.command(
        name="balance",
        description="Get your points balance or another user's point balance",
        guild_ids=[GUILD_ID],
    )
    async def balance(self, ctx, user: discord.Option(discord.User, default=None)):
        await ctx.defer()

        target_user = user if user else ctx.user

        sql = "SELECT points FROM users WHERE discordid = %s;"
        data = [target_user.id]
        result = await db.fetch_one(sql, data)

        points = result[0] if result else 0
        embed = discord.Embed(
            title=f"{target_user.display_name}'s points",
            description=f"{points} points",
            color=discord.Color.from_rgb(78, 42, 132),
        )
        await ctx.followup.send(embed=embed)

    @points_prediction.command(
        name="start", description="Start a prediction", guild_ids=[GUILD_ID]
    )
    async def start_prediction(self, ctx, title: str, option_a: str, option_b: str):
        # Check both in-memory and database for existing prediction
        if ctx.user.id in self.predictions:
            await ctx.respond("You already have a prediction open.", ephemeral=True)
            return
        existing = await get_active_prediction_by_creator(ctx.user.id)
        if existing:
            await ctx.respond("You already have a prediction open.", ephemeral=True)
            return
        if option_a == option_b:
            await ctx.respond("Options must be different.", ephemeral=True)
            return

        message = await ctx.send(f"PREDICTION: **{title}**")
        thread = await message.create_thread(name=f"PREDICTION: {title}")

        prediction = Prediction(title, option_a, option_b, thread)
        await prediction.create_prediction()

        # Save to database
        db_id = await create_prediction_db(
            ctx.user.id, title, option_a, option_b, thread.id, prediction.message.id
        )
        prediction.db_id = db_id
        prediction.view.prediction_id = db_id

        self.predictions[ctx.user.id] = prediction
        await ctx.respond(f"Prediction started: {thread.mention}", ephemeral=True)

    @points_prediction.command(
        name="lock",
        description="Lock prediction and stop further users from joining",
        guild_ids=[GUILD_ID],
    )
    async def lock_prediction(self, ctx):
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return

        await prediction.lock_prediction()
        await update_prediction_status(prediction.db_id, "locked")
        await ctx.respond("Prediction locked.", ephemeral=True)

    @points_prediction.command(
        name="complete",
        description="Complete prediction and reward users",
        guild_ids=[GUILD_ID],
    )
    async def complete_prediction(self, ctx, winner: str):
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return
        if winner not in [prediction.option_a, prediction.option_b]:
            await ctx.respond(
                f"Winner must be one of the options: `{prediction.option_a}` or `{prediction.option_b}`",
                ephemeral=True,
            )
            return

        await prediction.complete_prediction(winner)
        await update_prediction_status(prediction.db_id, "completed", winner)
        del self.predictions[ctx.user.id]

        await ctx.respond(f"Prediction completed for {winner}.", ephemeral=True)

    @points_prediction.command(
        name="refund",
        description="Cancel prediction and refund users",
        guild_ids=[GUILD_ID],
    )
    async def cancel_prediction(self, ctx):
        prediction = self.predictions.get(ctx.user.id, None)
        if not prediction:
            await ctx.respond("You don't have a prediction open.", ephemeral=True)
            return

        await prediction.refund_prediction()
        await update_prediction_status(prediction.db_id, "refunded")
        del self.predictions[ctx.user.id]

        await ctx.respond("Prediction refunded.", ephemeral=True)

    @tasks.loop(seconds=60)
    async def update_points(self):
        if not self.points_buffer:
            return

        sql = """INSERT INTO users (discordid, points)
            VALUES (%s, %s)
            ON CONFLICT (discordid)
            DO UPDATE SET points = users.points + EXCLUDED.points;
        """
        data = [(user_id, points) for user_id, points in self.points_buffer.items()]
        await db.perform_many(sql, data)

        self.points_buffer.clear()


def setup(bot):
    bot.add_cog(Points(bot))


class Prediction:
    def __init__(self, title, option_a, option_b, thread):
        self.title = title
        self.option_a = option_a
        self.option_b = option_b
        self.thread = thread
        self.db_id = None

    @classmethod
    def from_database(
        cls,
        db_id,
        title,
        option_a,
        option_b,
        thread,
        message_id,
        option_a_points,
        option_b_points,
        locked,
    ):
        """Restore a prediction from database data."""
        prediction = cls(title, option_a, option_b, thread)
        prediction.db_id = db_id
        prediction._message_id = message_id
        prediction._option_a_points = option_a_points
        prediction._option_b_points = option_b_points
        prediction._locked = locked
        return prediction

    async def restore_view(self):
        """Restore the view for a prediction loaded from database."""
        try:
            self.message = await self.thread.fetch_message(self._message_id)
        except discord.NotFound:
            # Original message deleted, create a new one
            self.message = None

        embed = discord.Embed(
            title=self.title,
            color=discord.Color.from_rgb(78, 42, 132),
        )
        self.view = PredictionView(
            self.option_a,
            self.option_b,
            embed,
            prediction_id=self.db_id,
            option_a_points=self._option_a_points,
            option_b_points=self._option_b_points,
            locked=self._locked,
        )

        if self.message:
            # Update existing message with new view
            await self.message.edit(embed=self.view.update_embed(), view=self.view)
        else:
            # Create new message since original was deleted
            self.message = await self.thread.send(
                "Prediction restored:", embed=self.view.update_embed(), view=self.view
            )

    async def create_prediction(self):
        embed = discord.Embed(
            title=self.title,
            color=discord.Color.from_rgb(78, 42, 132),
        )
        self.view = PredictionView(self.option_a, self.option_b, embed)
        self.message = await self.thread.send(
            "", embed=self.view.update_embed(), view=self.view
        )

    async def lock_prediction(self):
        if self.view.locked:
            return
        await self.view.lock_view()
        await self.message.reply("Prediction locked.")

    async def complete_prediction(self, winner):
        if not self.view.option_a_points or not self.view.option_b_points:
            sql = "UPDATE users SET points = points + %s WHERE discordid = %s;"
            data = [
                (points, user_id)
                for user_id, points in self.view.option_a_points.items()
            ] + [
                (points, user_id)
                for user_id, points in self.view.option_b_points.items()
            ]
            await db.perform_many(sql, data)
            await self.view.lock_view()
            await self.message.reply("Everyone voted the same way! Points refunded.")
            return

        sql = "UPDATE users SET points = points + %s WHERE discordid = %s;"
        if winner == self.option_a:
            payout = self.view.odds_a
            data = [
                (round(points * payout), user_id)
                for user_id, points in self.view.option_a_points.items()
            ]
        else:
            payout = self.view.odds_b
            data = [
                (round(points * payout), user_id)
                for user_id, points in self.view.option_b_points.items()
            ]
        await db.perform_many(sql, data)
        format = "Prediction completed -- {} points distributed to {} ({}x payout)."
        if winner == self.option_a:
            message = format.format(
                sum(self.view.option_b_points.values()),
                self.option_a,
                round(payout, 2),
            )
        else:
            message = format.format(
                sum(self.view.option_a_points.values()),
                self.option_b,
                round(payout, 2),
            )
        await self.view.lock_view()
        await self.message.reply(message)

    async def refund_prediction(self):
        sql = "UPDATE users SET points = points + %s WHERE discordid = %s;"
        data = [
            (points, user_id) for user_id, points in self.view.option_a_points.items()
        ] + [(points, user_id) for user_id, points in self.view.option_b_points.items()]
        await db.perform_many(sql, data)
        await self.view.lock_view()
        await self.message.reply("Prediction cancelled. Points refunded.")


class PredictionView(discord.ui.View):
    def __init__(
        self,
        option_a,
        option_b,
        embed,
        prediction_id=None,
        option_a_points=None,
        option_b_points=None,
        locked=False,
    ):
        super().__init__(timeout=None if locked else 1200)

        self.option_a = option_a
        self.option_a_points = option_a_points if option_a_points else {}
        self.option_b = option_b
        self.option_b_points = option_b_points if option_b_points else {}

        self.message = None
        self.embed = embed
        self.locked = locked
        self.prediction_id = prediction_id

        def create_button(label):
            async def button_callback(interaction):
                if any(
                    [
                        label == self.option_a
                        and interaction.user.id in self.option_b_points,
                        label == self.option_b
                        and interaction.user.id in self.option_a_points,
                    ]
                ):
                    await interaction.response.send_message(
                        f"{interaction.user.mention} tried to change sides..."
                    )
                    return
                sql = "SELECT points FROM users WHERE discordid = %s;"
                data = [interaction.user.id]
                result = await db.fetch_one(sql, data)

                await interaction.response.send_modal(
                    PredictionModal(
                        self.modal_callback, label, result[0] if result else 0
                    )
                )

            button = discord.ui.Button(label=label, disabled=locked)
            button.callback = button_callback
            return button

        self.add_item(create_button(self.option_a))
        self.add_item(create_button(self.option_b))

    def update_embed(self):
        # TODO: add odds
        self.embed.clear_fields()
        format = "{} points\n{} users\n{}x payout"
        self.odds_a = (
            1
            + (sum(self.option_b_points.values()) / sum(self.option_a_points.values()))
            if self.option_a_points
            else 1
        )
        self.odds_b = (
            1
            + (sum(self.option_a_points.values()) / sum(self.option_b_points.values()))
            if self.option_b_points
            else 1
        )
        self.embed.add_field(
            name=self.option_a,
            value=format.format(
                sum(self.option_a_points.values()),
                len(self.option_a_points),
                round(self.odds_a, 2),
            ),
        )
        self.embed.add_field(
            name=self.option_b,
            value=format.format(
                sum(self.option_b_points.values()),
                len(self.option_b_points),
                round(self.odds_b, 2),
            ),
        )
        return self.embed

    async def on_timeout(self):
        if self.locked:
            return
        await self.message.reply("Prediction locked.")
        await self.lock_view()

    async def lock_view(self):
        self.locked = True
        self.disable_all_items()
        await self.message.edit(view=self)

    async def modal_callback(self, user, points, option):
        if option == self.option_a:
            prev = self.option_a_points.pop(user.id, 0)
            self.option_a_points[user.id] = prev + points
        else:
            prev = self.option_b_points.pop(user.id, 0)
            self.option_b_points[user.id] = prev + points

        await self.message.edit(embed=self.update_embed())

        format = "{} bet {} points on **{}**"
        format_prev = "\n(up from {})"
        message = format.format(user.mention, prev + points, option)
        if prev > 0:
            message += format_prev.format(prev)

        sql = "UPDATE users SET points = points - %s WHERE discordid = %s;"
        data = [points, user.id]
        await db.perform_one(sql, data)

        # Persist bet to database
        if self.prediction_id:
            await place_bet_db(self.prediction_id, user.id, option, points)

        await self.message.reply(message)


class PredictionModal(discord.ui.Modal):
    def __init__(self, callback, option, user_points):
        super().__init__(title="Prediction")
        self.view_callback = callback
        self.option = option
        self.user_points = user_points

        self.add_item(
            discord.ui.InputText(
                label=f"How many points? ({self.user_points} available)",
                required=True,
                min_length=1,
                placeholder="Enter a number greater than 0",
            )
        )

    async def callback(self, interaction):
        value = self.children[0].value
        if not value.isdigit():
            await interaction.response.send_message(
                "You must wager a numeric amount!", ephemeral=True
            )
            return

        points = int(value)
        if points <= 0:
            await interaction.response.send_message(
                "You must wager more than 0 points!", ephemeral=True
            )
            return

        if self.user_points < points:
            await interaction.response.send_message(
                "You don't have enough points!", ephemeral=True
            )
            return

        await interaction.response.defer()
        await self.view_callback(interaction.user, points, self.option)
