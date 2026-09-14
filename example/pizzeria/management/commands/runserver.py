from __future__ import annotations

# The staticfiles version, which also serves the server's icon in DEBUG mode.
from django.contrib.staticfiles.management.commands.runserver import (
    Command as RunserverCommand,
)


class Command(RunserverCommand):
    default_port = "8066"
