"""s2studio, Python half: registers the small-model task that turns a sentence into a live-action intent."""


def register(ctx):
    ctx.register_auxiliary_task(
        "s2studio_intent", display_name="Source 2 Studio intents",
        description="Turns one sentence about a running CS2 game into s2docs' live-action JSON.")
