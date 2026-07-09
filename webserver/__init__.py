# The activity server is a standalone service (webserver/activity_app.py), no longer
# started in-process by the bot. Kept import-light so submodules (redis_state,
# spectate, activity) can import the package without cycles.
